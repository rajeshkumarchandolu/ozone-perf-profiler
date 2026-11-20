"""Read-only checks run on cluster hosts over ssh. Each pairs a bash snippet with a pure parser.

Only `kinit` writes anything: a ticket cache in its own file (KRB5CC), so the ssh user's default cache is untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .remote import Remote, RemoteError, short
from .topology import Role

KRB5CC = "FILE:/tmp/opp_krb5cc"

# Args: pairs of <role type> <main class>. One PROC line per java process running that class.
_JVM_PROBE = r'''
set -u
while (($# >= 2)); do
  type=$1 cls=$2; shift 2
  for pid in $(pgrep -f -- "$cls" || true); do
    exe=$(readlink -f "/proc/$pid/exe" 2> /dev/null) || continue
    [[ ${exe##*/} == java ]] || continue  # pgrep also matches this shell, whose args contain the class
    bin=${exe%/*}
    if [[ -x $bin/jcmd ]]; then jcmd=$bin/jcmd
    elif [[ -x $bin/../../bin/jcmd ]]; then jcmd=$(readlink -f "$bin/../../bin/jcmd")
    else jcmd=-; fi
    user=$(stat -c %U "/proc/$pid" 2> /dev/null || echo -)
    up=$(ps -o etimes= -p "$pid" 2> /dev/null | tr -d ' ')
    cwd=$(readlink "/proc/$pid/cwd" 2> /dev/null || echo -)
    printf 'PROC\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$type" "$pid" "$user" "${up:-0}" "$exe" "$jcmd" "$cwd"
  done
done
'''

_OZONE_CLIENT = r'''
command -v ozone > /dev/null || { echo "ozone CLI not found in PATH" >&2; exit 3; }
ozone version 2> /dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*' | head -1
'''

# Args: config keys. Prints "<key>\t<value>"; a missing key prints a message with spaces, treated as unset.
_GETCONF = r'''
for k; do printf '%s\t%s\n' "$k" "$(ozone getconf -confKey "$k" 2> /dev/null | tail -1)"; done
'''

# Args: <dir>. Prints "<keytab>\t<principal>" for each principal of each readable *.keytab in dir.
_KEYTAB_INVENTORY = r'''
for f in "$1"/*.keytab; do
  [[ -r $f ]] || continue
  klist -kt "$f" 2> /dev/null | awk -v f="$f" 'NR > 3 && NF {print f "\t" $NF}'
done | awk '!seen[$0]++'
'''

_KEYTAB_PRINCIPALS = r'''
[[ -r $1 ]] || { echo "keytab $1 not readable" >&2; exit 3; }
klist -kt "$1" | awk 'NR > 3 && NF {print $NF}' | awk '!seen[$0]++'
'''

# Args: <ccache> <keytab> <principal>. Prints the TGT line of klist.
_KINIT = r'''
export KRB5CCNAME=$1
kinit -kt "$2" "$3" && klist | grep -m1 krbtgt/
'''

# Args: <ccache> <om service id or empty>.
_OM_ROLES = r'''
export KRB5CCNAME=$1
ozone admin om roles ${2:+-id "$2"}
'''

# Args: <ccache>.
_SCM_ROLES = r'''
export KRB5CCNAME=$1
ozone admin scm roles
'''

# Readable and valid inside Ozone volume names (lowercase, digits, '-'), e.g. 2026-nov-11-20h15m03s.
_RUN_NAME = "LC_ALL=C date +%Y-%b-%d-%Hh%Mm%Ss | tr '[:upper:]' '[:lower:]'"

SECURITY_KEYS = ("hadoop.security.authentication", "ozone.security.enabled", "ozone.security.http.policy",
                 "hdds.grpc.tls.enabled", "ozone.om.service.ids")


@dataclass(frozen=True)
class Jvm:
    pid: int
    user: str
    java: str
    jcmd: str | None
    uptime_s: int


@dataclass(frozen=True)
class Security:
    kerberos: bool
    ozone_security: bool
    http_policy: str
    grpc_tls: bool
    om_service_id: str | None

    def describe(self) -> str:
        return (f"Kerberos {'on' if self.kerberos else 'off'}, Ozone security {'on' if self.ozone_security else 'off'}"
                f", HTTP {self.http_policy or 'HTTP_ONLY'}, gRPC TLS {'on' if self.grpc_tls else 'off'}")


# --- JVMs ---------------------------------------------------------------------------------------------------------

def probe_jvms(remote: Remote, host: str, roles: list[Role]) -> dict[str, Jvm]:
    """role name -> JVM, for the roles on host whose process was found."""
    types = sorted({r.type for r in roles})
    args = [a for t in types for a in (t, next(r.main_class for r in roles if r.type == t))]
    procs = parse_jvm_probe(remote.run(host, _JVM_PROBE, *args))
    return {r.name: jvm for r in roles if (jvm := pick_jvm(r.type, procs.get(r.type, [])))}


def parse_jvm_probe(out: str) -> dict[str, list[tuple[Jvm, str]]]:
    """role type -> [(JVM, cwd)]."""
    procs: dict[str, list[tuple[Jvm, str]]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 8 and parts[0] == "PROC":
            _, rtype, pid, user, up, exe, jcmd, cwd = parts
            jvm = Jvm(int(pid), user, exe, None if jcmd == "-" else jcmd, int(up) if up.isdigit() else 0)
            procs.setdefault(rtype, []).append((jvm, cwd))
    return procs


def pick_jvm(role_type: str, candidates: list[tuple[Jvm, str]]) -> Jvm | None:
    """CM starts each role from a process dir named after its type; prefer that, then the youngest process."""
    in_cm_dir = [c for c in candidates if role_type in c[1]]
    ranked = sorted(in_cm_dir or candidates, key=lambda c: c[0].uptime_s)
    return ranked[0][0] if ranked else None


# --- Ozone client on the freon host -------------------------------------------------------------------------------

def ozone_client_version(remote: Remote, host: str) -> str:
    version = remote.run(host, _OZONE_CLIENT, timeout=90).strip()
    if not version:
        raise RemoteError(f"{short(host)}: 'ozone version' printed no version")
    return version


def security(remote: Remote, host: str) -> Security:
    return parse_security(remote.run(host, _GETCONF, *SECURITY_KEYS, timeout=180))


def parse_security(out: str) -> Security:
    conf = {}
    for line in out.splitlines():
        key, _, value = line.partition("\t")
        conf[key] = "" if " " in value.strip() else value.strip()
    service_ids = [s for s in conf.get("ozone.om.service.ids", "").split(",") if s]
    return Security(
        kerberos=conf.get("hadoop.security.authentication", "").lower() == "kerberos",
        ozone_security=conf.get("ozone.security.enabled", "").lower() == "true",
        http_policy=conf.get("ozone.security.http.policy", ""),
        grpc_tls=conf.get("hdds.grpc.tls.enabled", "").lower() == "true",
        om_service_id=service_ids[0] if service_ids else None,
    )


def run_name(remote: Remote, host: str) -> str:
    """The run's name from the Freon host's clock and time zone, so it matches that host's logs."""
    name = remote.run(host, _RUN_NAME, timeout=30).strip()
    if not re.fullmatch(r"\d{4}-[a-z]{3}-\d{2}-\d{2}h\d{2}m\d{2}s", name):
        raise RemoteError(f"{short(host)}: unexpected date output {name!r}")
    return name


# --- Kerberos -----------------------------------------------------------------------------------------------------

OZONE_KEYTABS = ("om", "scm", "dn", "s3g", "recon")  # listed first


def keytab_inventory(remote: Remote, host: str, directory: str) -> list[tuple[str, str]]:
    return parse_keytab_inventory(remote.run(host, _KEYTAB_INVENTORY, directory, timeout=60))


def parse_keytab_inventory(out: str) -> list[tuple[str, str]]:
    """[(keytab path, principal)], Ozone service keytabs first, then by name."""
    entries = [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]

    def rank(entry: tuple[str, ...]) -> tuple[int, str]:
        name = entry[0].rsplit("/", 1)[-1].removesuffix(".keytab")
        return (OZONE_KEYTABS.index(name) if name in OZONE_KEYTABS else len(OZONE_KEYTABS), name)
    return sorted(entries, key=rank)  # type: ignore[return-value]


def keytab_principals(remote: Remote, host: str, keytab: str) -> list[str]:
    principals = remote.run(host, _KEYTAB_PRINCIPALS, keytab).split()
    if not principals:
        raise RemoteError(f"{short(host)}: no principals in {keytab}")
    return principals


def kinit(remote: Remote, host: str, keytab: str, principal: str) -> str:
    """Returns the klist TGT line (valid from / expires / principal)."""
    return " ".join(remote.run(host, _KINIT, KRB5CC, keytab, principal).split())


# --- OM and SCM HA ------------------------------------------------------------------------------------------------

_OM_ROLE_LINE = re.compile(r"(\S+)\s*:\s*(LEADER|FOLLOWER|LISTENER)\s*\(([^)]+)\)")  # om1 : LEADER (host)
_SCM_ROLE_LINE = re.compile(r"^\s*([^\s:]+):\d+:(LEADER|FOLLOWER|CANDIDATE|LISTENER)\b", re.M)  # host:port:LEADER:...


def om_leader(remote: Remote, host: str, service_id: str | None) -> str | None:
    """Hostname of the OM leader, or None when it can't be told."""
    return parse_om_leader(remote.run(host, _OM_ROLES, KRB5CC, service_id or "", timeout=120))


def parse_om_leader(out: str) -> str | None:
    return next((m.group(3) for m in _OM_ROLE_LINE.finditer(out) if m.group(2) == "LEADER"), None)


def scm_leader(remote: Remote, host: str) -> str | None:
    """Hostname of the SCM (Ratis) leader, or None when it can't be told."""
    return parse_scm_leader(remote.run(host, _SCM_ROLES, KRB5CC, timeout=120))


def parse_scm_leader(out: str) -> str | None:
    return next((m.group(1) for m in _SCM_ROLE_LINE.finditer(out) if m.group(2) == "LEADER"), None)
