"""Cluster discovery: find the Ozone service through CM, check ssh and the Freon client host, save cluster.json."""

from __future__ import annotations

import dataclasses
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from cmclient import cm
from cmclient.cm import CmClient, CmCluster, CmError, CmService, CmTlsError
from cmclient.http import Tls

from . import probes, topology
from .manifest import RunDir
from .options import CM_PWD_ENV, SSH_PWD_ENV, Args
from .probes import Jvm, Security
from .prompts import Abort, Invalid, Ok, Option, Prompter
from .remote import Remote, RemoteError, parallel, short
from .topology import Topology

T = TypeVar("T")


@dataclass(frozen=True)
class KerberosLogin:
    keytab: str  # path on the freon host
    principal: str
    ccache: str
    tgt: str


@dataclass(frozen=True)
class Discovery:
    topology: Topology
    jvms: dict[str, Jvm]  # role name -> JVM
    freon_host: str
    ozone_version: str
    security: Security
    kerberos: KerberosLogin | None
    om_leader: str | None
    scm_leader: str | None

    @property
    def leaders(self) -> dict[str, str]:
        """role short name -> leader host, for the services that have one."""
        return {k: v for k, v in (("om", self.om_leader), ("scm", self.scm_leader)) if v}


@dataclass
class Discovered:
    """Discovery output handed to the next steps. The caller owns (and closes) the ssh connection."""
    discovery: Discovery
    remote: Remote
    run_dir: RunDir
    answers: dict[str, object]  # flags that reproduce phase 0
    tls: Tls                    # the TLS trust chosen for CM, reused for the cluster's Prometheus


def run(p: Prompter, args: Args) -> Discovered:
    p.section("Cluster discovery")
    client, tls = _login_cm(p, args)
    cluster, service = _pick_service(p, args, client)
    topo = _load_topology(p, client, cluster, service)
    ssh_user = p.ask("ssh user", default="root", preset=args.ssh_user, flag="--ssh-user")
    remote = _ssh_login(p, ssh_user, topo.hosts)
    try:
        jvms = _probe_jvms(p, remote, topo)
        freon_host, version = _pick_freon_host(p, args, remote, topo)
        security = _security(p, remote, freon_host)
        kerberos = _kerberos(p, args, remote, freon_host) if security.kerberos else None
        if args.om_leader:
            _pin_leader(p, "OM", "--om-leader", args.om_leader, topo.of_type("OZONE_MANAGER"),
                        lambda: probes.om_roles(remote, freon_host, security.om_service_id),
                        lambda node: probes.om_transfer(remote, freon_host, security.om_service_id, node))
        if args.scm_leader:
            _pin_leader(p, "SCM", "--scm-leader", args.scm_leader, topo.of_type("STORAGE_CONTAINER_MANAGER"),
                        lambda: probes.scm_roles(remote, freon_host),
                        lambda node: probes.scm_transfer(remote, freon_host, node))
        om_leader = _leader(p, "OM", topo.of_type("OZONE_MANAGER"),
                            lambda: probes.om_leader(remote, freon_host, security.om_service_id))
        scm_leader = _leader(p, "SCM", topo.of_type("STORAGE_CONTAINER_MANAGER"),
                             lambda: probes.scm_leader(remote, freon_host))
        found = Discovery(topo, jvms, freon_host, version, security, kerberos, om_leader, scm_leader)
        _print_layout(p, found)
        if not p.confirm("\nSave this cluster layout?"):
            raise Abort("layout not confirmed")
        name = args.run_name or _run_name(remote, freon_host)
        run_dir, answers = _save(p, args, name, found, client, tls, ssh_user)
    except BaseException:
        remote.close()
        raise
    return Discovered(found, remote, run_dir, answers, tls)


# --- CM -----------------------------------------------------------------------------------------------------------

def _login_cm(p: Prompter, args: Args) -> tuple[CmClient, Tls]:
    tls = Tls(args.cm_ca_cert, args.insecure)
    url, trusted = p.ask("Cloudera Manager URL", _reachable(tls), preset=args.cm_url, flag="--cm-url")
    if not trusted:
        p.warn("the CM TLS certificate is not trusted by this machine (usual for CM Auto-TLS)")
        tls = p.ask("CA certificate (PEM path), or 'insecure' to skip verification", _trust(url),
                    default="insecure", flag="--cm-ca-cert or --insecure")
    user = p.ask("CM user", default="admin", preset=args.cm_user, flag="--cm-user")
    client = p.ask("CM password", _cm_login(url, tls, user), preset=os.environ.get(CM_PWD_ENV),
                   flag=f"${CM_PWD_ENV}", secret=True)
    return client, tls


def _reachable(tls: Tls) -> Callable[[str], Ok[tuple[str, bool]]]:
    def check(raw: str) -> Ok[tuple[str, bool]]:
        try:
            url = cm.normalize_url(raw)
        except ValueError as e:
            raise Invalid(str(e)) from None
        try:
            cm.probe(url, tls)
        except CmTlsError:
            return Ok((url, False))
        except CmError as e:
            raise Invalid(f"{url}: {e}") from None
        return Ok((url, True), f"{url} is reachable")
    return check


def _trust(url: str) -> Callable[[str], Ok[Tls]]:
    def check(raw: str) -> Ok[Tls]:
        tls = Tls(insecure=True) if raw.lower() == "insecure" else Tls(ca_cert=str(Path(raw).expanduser()))
        try:
            tls.context()  # fails fast on a missing or unreadable PEM
            cm.probe(url, tls)
        except OSError as e:
            raise Invalid(f"cannot load {raw}: {e}") from None
        except CmError as e:
            raise Invalid(str(e)) from None
        return Ok(tls, "TLS still encrypts, but the CM certificate is not verified" if tls.insecure
                  else f"TLS verified with {tls.ca_cert}")
    return check


def _cm_login(url: str, tls: Tls, user: str) -> Callable[[str], Ok[CmClient]]:
    def check(password: str) -> Ok[CmClient]:
        client = CmClient(url, tls, user, password)
        try:
            version = client.login()
        except CmError as e:
            raise Invalid(str(e)) from None
        return Ok(client, f"logged in as {user}, CM API {version}")
    return check


def _pick_service(p: Prompter, args: Args, client: CmClient) -> tuple[CmCluster, CmService]:
    clusters = _cm_call(client.clusters)
    cluster = p.choose("Clusters", [Option(c.name, f"{c.display_name}  ({c.name}, version {c.version})", c)
                                    for c in clusters], preset=args.cluster, flag="--cluster")
    services = _cm_call(lambda: client.ozone_services(cluster))
    if not services:
        raise Abort(f"cluster {cluster.display_name} has no Ozone service")
    service = p.choose("Ozone services", [Option(s.name, f"{s.display_name}  ({s.state}, health {s.health})", s)
                                          for s in services], preset=args.service, flag="--service")
    return cluster, service


def _load_topology(p: Prompter, client: CmClient, cluster: CmCluster, service: CmService) -> Topology:
    topo = topology.build(client.base_url, client.api_version, cluster, service,
                          _cm_call(lambda: client.roles(cluster, service)), _cm_call(client.hostnames))
    counts = ", ".join(f"{n} {kind}" for kind, n in topo.counts().items())
    p.ok(f"{service.display_name}: {len(topo.roles)} roles on {len(topo.hosts)} hosts ({counts})")
    for problem in topo.problems():
        p.warn(problem)
    prom_role = next(iter(topo.of_type("OZONE_PROMETHEUS")), None)
    if prom_role:
        endpoint = topology.prometheus_endpoint(prom_role.host,
                                                _cm_call(lambda: client.role_config(cluster, service,
                                                                                    prom_role.name)))
        if endpoint:
            topo = dataclasses.replace(topo, prometheus_url=endpoint[0], prometheus_source=endpoint[1])
            p.ok(f"Prometheus at {endpoint[0]} (from CM {endpoint[1]})")
    if not topo.prometheus_url:
        p.warn("no Ozone Prometheus role found in CM; the metrics phase will ask for its URL")
    return topo


def _cm_call(fn: Callable[[], T]) -> T:
    try:
        return fn()
    except CmError as e:
        raise Abort(f"Cloudera Manager: {e}") from None


# --- ssh and hosts ------------------------------------------------------------------------------------------------

def _ssh_login(p: Prompter, user: str, hosts: list[str]) -> Remote:
    def check(password: str) -> Ok[Remote]:
        remote = Remote(user, password)
        failed = {h: e for h, e in parallel(remote.check, hosts).items() if isinstance(e, Exception)}
        if failed:
            remote.close()
            first = next(iter(failed.values()))
            raise Invalid(f"ssh failed on {len(failed)}/{len(hosts)} hosts "
                          f"({', '.join(short(h) for h in failed)}): {first}")
        return Ok(remote, f"ssh ok on {len(hosts)}/{len(hosts)} hosts as {user}"
                          + ("" if password else " (keys)"))
    return p.ask("ssh password (empty = ssh keys)", check, default="", preset=os.environ.get(SSH_PWD_ENV),
                 flag=f"${SSH_PWD_ENV}", secret=True)


def _probe_jvms(p: Prompter, remote: Remote, topo: Topology) -> dict[str, Jvm]:
    roles = topo.jvm_roles
    by_host = {h: [r for r in roles if r.host == h] for h in sorted({r.host for r in roles})}
    jvms: dict[str, Jvm] = {}
    for host, result in parallel(lambda h: probes.probe_jvms(remote, h, by_host[h]), by_host).items():
        if isinstance(result, Exception):
            p.warn(f"JVM probe failed on {short(host)}: {result}")
        else:
            jvms.update(result)
    for r in roles:
        if r.name not in jvms:
            p.warn(f"no running JVM for {r.short} on {short(r.host)}")
        elif not jvms[r.name].jcmd:
            p.warn(f"{r.short} on {short(r.host)}: no jcmd next to {jvms[r.name].java} (JFR won't work)")
    p.ok(f"{len(jvms)}/{len(roles)} Ozone JVMs found")
    return jvms


def _pick_freon_host(p: Prompter, args: Args, remote: Remote, topo: Topology) -> tuple[str, str]:
    hosts = topo.freon_candidates() or topo.hosts

    def label(host: str) -> str:
        servers = topo.servers_on(host)
        return f"{host}  " + (f"(also runs {', '.join(servers)})" if servers else "(no OM/SCM/DN, recommended)")

    def has_client(host: str) -> Ok[tuple[str, str]]:
        try:
            version = probes.ozone_client_version(remote, host)
        except RemoteError as e:
            raise Invalid(str(e)) from None
        return Ok((host, version), f"Ozone client {version} on {short(host)}")

    host, version = p.choose("Freon client hosts (Ozone gateways)", [Option(h, label(h), h) for h in hosts],
                             preset=args.freon_host, flag="--freon-host", validate=has_client)
    if servers := topo.servers_on(host):
        p.warn(f"{short(host)} also runs {', '.join(servers)}: the Freon client load shows in its node metrics")
    return host, version


def _security(p: Prompter, remote: Remote, host: str) -> Security:
    try:
        security = probes.security(remote, host)
    except RemoteError as e:
        raise Abort(f"reading the Ozone client configuration failed: {e}") from None
    p.ok(security.describe())
    return security


def _kerberos(p: Prompter, args: Args, remote: Remote, host: str) -> KerberosLogin:
    """Pick a keytab + principal from --keytab-dir on the freon host; typing a path is the fallback."""
    def login(keytab: str, principal: str) -> Ok[KerberosLogin]:
        try:
            tgt = probes.kinit(remote, host, keytab, principal)
        except RemoteError as e:
            raise Invalid(f"kinit as {principal} failed: {e}") from None
        return Ok(KerberosLogin(keytab, principal, probes.KRB5CC, tgt), f"kinit ok: {tgt}")

    if args.kerberos_keytab is None:
        try:
            entries = probes.keytab_inventory(remote, host, args.keytab_dir)
        except RemoteError as e:
            p.warn(f"listing {args.keytab_dir} failed: {e}")
            entries = []
        if entries:
            options = [Option(principal, f"{Path(keytab).name:<22} {principal}", (keytab, principal))
                       for keytab, principal in entries]
            options.append(Option("other", "other keytab path ...", None))
            picked = p.choose(f"Keytabs in {args.keytab_dir} on {short(host)}", options,
                              preset=args.kerberos_principal, flag="--kerberos-principal",
                              validate=lambda e: login(*e) if e else Ok(None))
            if picked:
                return picked
        else:
            p.warn(f"no readable *.keytab in {args.keytab_dir} on {short(host)}")

    def principals(keytab: str) -> Ok[tuple[str, list[str]]]:
        try:
            found = probes.keytab_principals(remote, host, keytab)
        except RemoteError as e:
            raise Invalid(str(e)) from None
        return Ok((keytab, found), f"{len(found)} principal(s) in {keytab}")

    keytab, names = p.ask(f"Keytab path on {short(host)}", principals, preset=args.kerberos_keytab,
                          flag="--kerberos-keytab")
    return p.choose("Principals", [Option(n, n, n) for n in names], preset=args.kerberos_principal,
                    flag="--kerberos-principal", validate=lambda n: login(keytab, n))


def _leader(p: Prompter, label: str, roles: list, ask_cluster: Callable[[], str | None]) -> str | None:
    """The Ratis leader of OM or SCM: the only instance, else what `ozone admin <om|scm> roles` says."""
    if len(roles) == 1:
        return roles[0].host
    try:
        leader = ask_cluster()
    except RemoteError as e:
        p.warn(f"cannot tell the {label} leader: {e}")
        return None
    if leader:
        p.ok(f"{label} leader: {leader}")
    else:
        p.warn(f"'ozone admin {label.lower()} roles' named no leader")
    return leader


PIN_ATTEMPTS = 3   # leadership transfers before giving up
PIN_WAIT_S = 20    # after each transfer, before checking again


def _pin_leader(p: Prompter, label: str, flag: str, want: str, servers: list,
                roles: Callable[[], list[tuple[str, str, str]]], transfer: Callable[[str], None]) -> None:
    """Makes the OM or SCM on host `want` the Ratis leader and checks that it is. roles() lists (node id, role, host)
    of every server; transfer(node id) hands leadership over (ozone admin om|scm transfer).
    Raises Abort when it isn't the leader after PIN_ATTEMPTS transfers: runs with another leader wouldn't compare."""
    target = next((r.host for r in servers if short(r.host) == short(want)), None)
    if target is None:
        raise Abort(f"{flag} {want}: no {label} there ({label}s: {', '.join(short(r.host) for r in servers)})")
    for attempt in range(PIN_ATTEMPTS + 1):
        try:
            listed = roles()
            node = next((n for n, _, h in listed if short(h) == short(target)), None)
            if node is None:
                raise Abort(f"{flag} {want}: 'ozone admin {label.lower()} roles' does not list it: {listed}")
            if any(role == "LEADER" and short(h) == short(target) for _, role, h in listed):
                p.ok(f"{label} leader pinned to {short(target)} ({node})")
                return
            if attempt == PIN_ATTEMPTS:
                break
            p.say(f"  transferring {label} leadership to {short(target)} ({node}), try {attempt + 1}/{PIN_ATTEMPTS}")
            transfer(node)
        except RemoteError as e:
            p.warn(f"{label} leadership transfer to {short(target)}: {e}")
        time.sleep(PIN_WAIT_S)
    raise Abort(f"{flag} {want}: still not the {label} leader after {PIN_ATTEMPTS} transfers")


# --- output -------------------------------------------------------------------------------------------------------

def _print_layout(p: Prompter, d: Discovery) -> None:
    def marks(host: str, kind: str) -> str:
        notes = []
        if kind in d.leaders and short(d.leaders[kind]) == short(host):
            notes.append("leader")
        if kind == "gateway" and host == d.freon_host:
            notes.append("freon")
        return ",".join(notes)

    rows = []
    for r in d.topology.roles:
        jvm = d.jvms.get(r.name)
        rows.append((r.short, short(r.host), r.state, r.health, r.config, jvm.pid if jvm else "-",
                     jvm.user if jvm else "-", _duration(jvm.uptime_s) if jvm else "-", marks(r.host, r.short)))
    p.say()
    p.table(("role", "host", "CM state", "health", "config", "PID", "user", "uptime", "notes"), rows)


def _duration(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _run_name(remote: Remote, host: str) -> str:
    try:
        return probes.run_name(remote, host)
    except RemoteError as e:
        raise Abort(f"reading the clock of {short(host)} for the run name failed: {e} (or pass --run-name)") from None


def _save(p: Prompter, args: Args, name: str, d: Discovery, client: CmClient, tls: Tls,
          ssh_user: str) -> tuple[RunDir, dict[str, object]]:
    try:
        run_dir = RunDir.create(args.out_dir, name)
    except (ValueError, FileExistsError) as e:
        raise Abort(str(e)) from None
    for name, body in client.raw.items():
        run_dir.write_text(f"cm/{name}.json", body)
    run_dir.write_json("cluster.json", d)
    answers = {
        "cm_url": client.base_url, "cm_user": client.user, "cm_ca_cert": tls.ca_cert,
        "insecure": tls.insecure, "cluster": d.topology.cluster.name, "service": d.topology.service.name,
        "ssh_user": ssh_user, "freon_host": d.freon_host,
        "kerberos_keytab": d.kerberos.keytab if d.kerberos else None,
        "kerberos_principal": d.kerberos.principal if d.kerberos else None,
        "om_leader": args.om_leader,
        "scm_leader": args.scm_leader,
    }
    run_dir.update_manifest("discover", answers)
    p.ok(f"saved {run_dir.path}/cluster.json")
    return run_dir, answers
