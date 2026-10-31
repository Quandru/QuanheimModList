"""Ask the Quanheim Valheim server for its details with Steam's standard server query (A2S_INFO).

    python3 tools/server_query.py            # address from the Gale profile's ServerConnect config
    python3 tools/server_query.py HOST:PORT  # a query port given directly

The address comes from BepInEx/config/zenox.serverconnect.cfg in the Gale profile download (Address = ip:gameport,
QueryPort = 0 meaning game port + 1), so it never has to be written into this public repo. In GitHub Actions the
host is masked in the log. Prints what the server reports (name, players, version, keywords) without the address,
and exits 1 if it doesn't answer. Standard library only.
"""
import io, json, os, re, socket, struct, sys, zipfile

REQUEST = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
SERVERCONNECT_CFG = "BepInEx/config/zenox.serverconnect.cfg"


def address_from_profile():
    """(host, query port) from the ServerConnect config inside the Gale profile."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import patchnotes as p
    z = zipfile.ZipFile(io.BytesIO(p.http(f"{p.GALE}/{p.PROFILE_CODE}")))
    cfg = z.read(SERVERCONNECT_CFG).decode("utf-8", "replace")
    host, game_port = re.search(r"^\s*Address\s*=\s*([^\s:]+):(\d+)", cfg, re.M).groups()
    m = re.search(r"^\s*QueryPort\s*=\s*(\d+)", cfg, re.M)
    query_port = int(m.group(1)) if m and int(m.group(1)) else int(game_port) + 1
    return host, query_port


def query(host, port, timeout=5.0):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(REQUEST, (host, port))
        data, _ = s.recvfrom(4096)
        if data[4:5] == b"A":                        # challenge: resend the request with it
            s.sendto(REQUEST + data[5:9], (host, port))
            data, _ = s.recvfrom(4096)
    return parse(data)


def parse(data):
    if data[:4] != b"\xFF\xFF\xFF\xFF" or data[4:5] != b"I":
        raise ValueError(f"unexpected reply: {data[:16]!r}")
    d, pos = data[5:], 1

    def string():
        nonlocal pos
        end = d.index(b"\0", pos)
        value = d[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return value

    info = {"protocol": d[0], "name": string(), "map": string(), "folder": string(), "game": string()}
    info["appId"], info["players"], info["maxPlayers"], info["bots"] = struct.unpack_from("<HBBB", d, pos)
    pos += 5
    info["serverType"], info["environment"] = chr(d[pos]), chr(d[pos + 1])
    info["passwordProtected"], info["vacSecured"] = bool(d[pos + 2]), bool(d[pos + 3])
    pos += 4
    info["version"] = string()
    if pos < len(d):                                 # extra data flags
        edf = d[pos]
        pos += 1
        if edf & 0x80:
            info["gamePort"] = struct.unpack_from("<H", d, pos)[0]
            pos += 2
        if edf & 0x10:
            info["steamId"] = str(struct.unpack_from("<Q", d, pos)[0])
            pos += 8
        if edf & 0x40:
            info["spectatorPort"] = struct.unpack_from("<H", d, pos)[0]
            pos += 2
            info["spectatorName"] = string()
        if edf & 0x20:
            info["keywords"] = string()
        if edf & 0x01:
            info["gameId"] = str(struct.unpack_from("<Q", d, pos)[0])
    return info


if __name__ == "__main__":
    if len(sys.argv) > 1:
        host, _, port = sys.argv[1].rpartition(":")
        port = int(port)
    else:
        host, port = address_from_profile()
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{host}")                 # keep the IP out of the public log
    try:
        result = query(host, port)
    except (OSError, ValueError) as e:
        print(f"No answer on query port {port} ({type(e).__name__}: {e})")
        sys.exit(1)
    print(f"Answered on query port {port}:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
