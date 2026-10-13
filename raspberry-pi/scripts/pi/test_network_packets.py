from __future__ import annotations

import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path


def run(*args: str) -> None:
    subprocess.run(args, check=True, timeout=10)


def connects(namespace: str, address: str, port: int) -> bool:
    result = subprocess.run(
        [
            "ip",
            "netns",
            "exec",
            namespace,
            "python3",
            "-c",
            "import socket,sys; socket.create_connection((sys.argv[1],int(sys.argv[2])),1).close()",
            address,
            str(port),
        ],
        capture_output=True,
        check=False,
        timeout=3,
    )
    return result.returncode == 0


def stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    process.wait(timeout=5)


def main() -> None:
    if not Path("/.dockerenv").is_file():
        raise RuntimeError("this test requires an isolated Docker container")
    with ExitStack() as stack:
        for port in (8090, 8091, 22):
            listener = stack.enter_context(socket.socket())
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("", port))
            listener.listen(16)
        for name, interface, address in (
            ("tablet", "ap0", "10.42.0"),
            ("internet", "wan0", "10.43.0"),
        ):
            run("ip", "netns", "add", name)
            stack.callback(run, "ip", "netns", "del", name)
            run("ip", "link", "add", interface, "type", "veth", "peer", "name", name)
            stack.callback(run, "ip", "link", "del", interface)
            run("ip", "link", "set", name, "netns", name)
            run("ip", "address", "add", f"{address}.1/24", "dev", interface)
            run("ip", "link", "set", interface, "up")
            run("ip", "netns", "exec", name, "ip", "address", "add", f"{address}.2/24", "dev", name)
            run("ip", "netns", "exec", name, "ip", "link", "set", name, "up")
        forwarding = Path("/proc/sys/net/ipv4/ip_forward")
        old_forwarding = forwarding.read_text()
        stack.callback(forwarding.write_text, old_forwarding)
        forwarding.write_text("1")
        run(
            "ip",
            "netns",
            "exec",
            "tablet",
            "ip",
            "route",
            "add",
            "10.43.0.0/24",
            "via",
            "10.42.0.1",
        )
        run(
            "ip",
            "netns",
            "exec",
            "internet",
            "ip",
            "route",
            "add",
            "10.42.0.0/24",
            "via",
            "10.43.0.1",
        )
        remote = subprocess.Popen(
            [
                "ip",
                "netns",
                "exec",
                "internet",
                "python3",
                "-u",
                "-c",
                "import socket\ns=socket.socket()\ns.bind(('10.43.0.2',8090))\ns.listen(16)\nprint('ready')\nwhile True:\n c,_=s.accept()\n c.close()",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        stack.callback(stop, remote)
        if remote.stdout is None or remote.stdout.readline().strip() != "ready":
            raise RuntimeError("forwarding target did not start")
        if not connects("tablet", "10.43.0.2", 8090):
            raise RuntimeError("forwarding baseline is broken before the firewall")
        run("nft", "-f", str(Path(sys.argv[1])))
        stack.callback(run, "nft", "delete", "table", "inet", "anheart")
        expected = (
            ("tablet", "10.42.0.1", 8090, True),
            ("tablet", "10.42.0.1", 22, True),
            ("tablet", "10.42.0.1", 8091, False),
            ("internet", "10.43.0.1", 8090, False),
            ("internet", "10.43.0.1", 22, False),
            ("internet", "10.43.0.1", 8091, False),
            ("tablet", "10.43.0.2", 8090, False),
        )
        for namespace, address, port, accepted in expected:
            observed = connects(namespace, address, port)
            if observed != accepted:
                raise RuntimeError(f"{namespace}:{port}: got {observed}, expected {accepted}")
        print(
            "PASS: real kernel packets, AP console/SSH accepted, other AP port and all Internet ports refused; routing blocked"
        )


if __name__ == "__main__":
    main()
