#!/usr/bin/env python3
"""扫描局域网内 UGOS Pro NAS（默认 HTTPS 9443）。"""
import argparse
import asyncio
import ipaddress
import ssl
import sys

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE


async def port_open(ip: str, port: int, sem: asyncio.Semaphore, timeout: float) -> str | None:
    async with sem:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
            return ip
        except Exception:
            return None


def fetch_fingerprint(ip: str, port: int) -> dict:
    """同步取一次 HTTPS 响应头与证书 CN。"""
    out: dict = {"ip": ip, "port": port}
    import http.client

    conn = http.client.HTTPSConnection(ip, port, timeout=6, context=ssl_ctx)
    try:
        conn.request("GET", "/")
        resp = conn.getresponse()
        out["status"] = resp.status
        out["server"] = resp.getheader("Server", "")
        out["location"] = resp.getheader("Location", "")
        body = resp.read(600).decode("utf-8", "ignore")
        low = body.lower()
        out["ugos_hint"] = any(k in low for k in ("ugos", "ugreen", "desktop", "绿联"))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    try:
        import socket

        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(6)
        wrapped = ssl_ctx.wrap_socket(raw, server_hostname=ip)
        wrapped.connect((ip, port))
        der = wrapped.getpeercert(binary_form=True)
        wrapped.close()
        if der:
            try:
                from cryptography import x509

                c = x509.load_der_x509_certificate(der)
                out["cert_cn"] = [a.value for a in c.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)]
                out["cert_org"] = [
                    a.value for a in c.subject.get_attributes_for_oid(x509.oid.NameOID.ORGANIZATION_NAME)
                ]
            except Exception:
                out["cert_cn"] = "unparsed"
    except Exception as e:
        out["cert_error"] = f"{type(e).__name__}: {e}"
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", default="192.0.2.0/24")
    ap.add_argument("--ports", default="9443")
    ap.add_argument("--timeout", type=float, default=0.9)
    ap.add_argument("--concurrency", type=int, default=256)
    args = ap.parse_args()

    ports = [int(p) for p in args.ports.split(",") if p.strip()]
    hosts = list(ipaddress.ip_network(args.net, strict=False).hosts())
    sem = asyncio.Semaphore(args.concurrency)

    hits: list[tuple[str, int]] = []
    for port in ports:
        tasks = [port_open(str(h), port, sem, args.timeout) for h in hosts]
        for ip in await asyncio.gather(*tasks):
            if ip:
                hits.append((ip, port))
                print(f"[open] {ip}:{port}", flush=True)

    if not hits:
        print("no open port found")
        return 1

    print("\n--- fingerprint ---")
    for ip, port in hits:
        info = fetch_fingerprint(ip, port)
        print(f"{ip}:{port} -> {info}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
