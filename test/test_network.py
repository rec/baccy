import logging
import subprocess
from pathlib import Path
from threading import Barrier
from typing import BinaryIO, cast

import pytest

from baccy.backup import run_backup
from baccy.models import ProjectUpload, Settings
from baccy.network import NetworkDiscovery, NetworkRecsSource


def test_network_discovery_tries_each_new_node_once(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if command[0] == 'arp':
            return subprocess.CompletedProcess(
                command, 0, b'? (192.168.1.3) at aa:bb:cc:dd:ee:ff on en0\n', b''
            )
        return subprocess.CompletedProcess(command, 1, b'', b'permission denied')

    discovery = NetworkDiscovery(run)

    assert discovery.discover() == []
    assert discovery.discover() == []
    assert len([command for command in calls if command[0] == 'ssh']) == 1


def test_network_discovery_normalizes_unpadded_mac_addresses() -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[0] == 'arp':
            return subprocess.CompletedProcess(
                command, 0, b'? (server.local) at 0:9:b0:4:72:df on en0\n', b''
            )
        return subprocess.CompletedProcess(command, 0, b'', b'')

    sources = NetworkDiscovery(run).discover()

    assert sources[0].mac == '00:09:b0:04:72:df'
    assert sources[0].name == 'network-0009b00472df'


def test_network_discovery_logs_verbose_host_results(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[0] == 'arp':
            return subprocess.CompletedProcess(
                command,
                0,
                (
                    b'? (recs.local) at aa:bb:cc:dd:ee:ff on en0\n'
                    b'? (other.local) at 00:11:22:33:44:55 on en0\n'
                    b'? (offline.local) at 66:77:88:99:aa:bb on en0\n'
                ),
                b'',
            )
        if command[-2] == 'recs.local':
            return subprocess.CompletedProcess(command, 0, b'', b'')
        if command[-2] == 'offline.local':
            return subprocess.CompletedProcess(command, 255, b'', b'connection refused')
        return subprocess.CompletedProcess(command, 1, b'', b'')

    caplog.set_level(logging.INFO, logger='baccy.network')

    sources = NetworkDiscovery(run, verbose=True, sleep=lambda delay: None).discover()

    assert [source.host for source in sources] == ['recs.local']
    assert 'network host recs.local (aa:bb:cc:dd:ee:ff) has recs' in caplog.text
    assert (
        'network host other.local (00:11:22:33:44:55) has no recs directory'
        in caplog.text
    )
    assert (
        'network SSH failed for offline.local (66:77:88:99:aa:bb): connection refused'
        in caplog.text
    )


def test_network_discovery_retries_connection_failures() -> None:
    calls = 0
    delays: list[float] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        if command[0] == 'arp':
            return subprocess.CompletedProcess(
                command, 0, b'? (pi.local) at aa:bb:cc:dd:ee:ff on en0\n', b''
            )
        calls += 1
        if calls < 3:
            return subprocess.CompletedProcess(command, 255, b'', b'connection refused')
        return subprocess.CompletedProcess(command, 0, b'', b'')

    sources = NetworkDiscovery(run, sleep=delays.append).discover()

    assert [source.host for source in sources] == ['pi.local']
    assert calls == 3
    assert delays == [2.0, 4.0]


def test_network_discovery_does_not_retry_authentication_rejection() -> None:
    calls = 0
    delays: list[float] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        if command[0] == 'arp':
            return subprocess.CompletedProcess(
                command, 0, b'? (pi.local) at aa:bb:cc:dd:ee:ff on en0\n', b''
            )
        calls += 1
        return subprocess.CompletedProcess(command, 255, b'', b'Permission denied')

    assert NetworkDiscovery(run, sleep=delays.append).discover() == []
    assert calls == 1
    assert delays == []


def test_network_discovery_rechecks_ssh_machines_without_recs() -> None:
    attempts = 0

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal attempts
        if command[0] == 'arp':
            return subprocess.CompletedProcess(
                command, 0, b'? (pi.local) at aa:bb:cc:dd:ee:ff on en0\n', b''
            )
        attempts += 1
        return subprocess.CompletedProcess(command, 1 if attempts == 1 else 0, b'', b'')

    discovery = NetworkDiscovery(run)

    assert discovery.discover() == []
    assert [machine.host for machine in discovery.new_machines] == ['pi.local']
    discovery.last_scan = None

    assert [source.host for source in discovery.discover()] == ['pi.local']
    assert attempts == 2


def test_network_recs_dry_run_does_not_write(tmp_path: Path) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[0] == 'arp':
            return subprocess.CompletedProcess(
                command, 0, b'? (192.168.1.3) at aa:bb:cc:dd:ee:ff on en0\n', b''
            )
        remote_command = command[-1]
        if remote_command == 'test -d "$HOME/recs"':
            return subprocess.CompletedProcess(command, 0, b'', b'')
        return subprocess.CompletedProcess(
            command,
            0,
            b'./session/recording.toml\x001\x0013\x00',
            b'',
        )

    destination = tmp_path / 'backup'
    result = run_backup(
        Settings(backup_root=destination, discover_removable=False),
        dry_run=True,
        network=NetworkDiscovery(run),
    )

    assert result.discovered == 1
    assert result.would_copy == 1
    assert result.results[0].source == 'network-aabbccddeeff'
    assert not destination.exists()


def test_network_recs_backup_skips_unchanged_files(tmp_path: Path) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[0] == 'arp':
            return subprocess.CompletedProcess(
                command, 0, b'? (192.168.1.3) at aa:bb:cc:dd:ee:ff on en0\n', b''
            )
        remote_command = command[-1]
        if remote_command == 'test -d "$HOME/recs"':
            return subprocess.CompletedProcess(command, 0, b'', b'')
        if 'find . -type f' in remote_command:
            return subprocess.CompletedProcess(
                command,
                0,
                b'./session/recording.toml\x001\x0016\x00',
                b'',
            )
        output = kwargs['stdout']
        cast(BinaryIO, output).write(b'format = "recs"\n')
        return subprocess.CompletedProcess(command, 0, b'', b'')

    destination = tmp_path / 'backup'
    settings = Settings(backup_root=destination, discover_removable=False)
    network = NetworkDiscovery(run)

    first = run_backup(settings, network=network)
    second = run_backup(settings, network=network)

    assert first.copied == 1
    assert second.unchanged == 1
    assert (
        destination / 'sources' / 'network-aabbccddeeff' / 'session' / 'recording.toml'
    ).read_text() == 'format = "recs"\n'


def test_network_discovery_ignores_multicast_and_broadcast_nodes() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            (
                b'? (239.0.0.250) at 01:00:5e:00:00:fa on en0\n'
                b'? (192.168.1.255) at ff:ff:ff:ff:ff:ff on en0\n'
            ),
            b'',
        )

    assert NetworkDiscovery(run).discover() == []
    assert calls == [['arp', '-an']]


def test_network_discovery_disables_host_key_checking() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if command[0] == 'arp':
            return subprocess.CompletedProcess(
                command, 0, b'? (pi.local) at aa:bb:cc:dd:ee:ff on en0\n', b''
            )
        return subprocess.CompletedProcess(command, 0, b'', b'')

    NetworkDiscovery(run).discover()

    assert calls[1] == [
        'ssh',
        '-o',
        'BatchMode=yes',
        '-o',
        'ConnectTimeout=1',
        '-o',
        'StrictHostKeyChecking=no',
        '-o',
        'UserKnownHostsFile=/dev/null',
        'pi.local',
        'test -d "$HOME/recs"',
    ]


def test_network_discovery_probes_new_hosts_in_parallel() -> None:
    barrier = Barrier(2)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[0] == 'arp':
            return subprocess.CompletedProcess(
                command,
                0,
                (
                    b'? (first.local) at aa:bb:cc:dd:ee:01 on en0\n'
                    b'? (second.local) at aa:bb:cc:dd:ee:02 on en0\n'
                ),
                b'',
            )
        barrier.wait(timeout=1)
        return subprocess.CompletedProcess(command, 0, b'', b'')

    sources = NetworkDiscovery(run).discover()

    assert [source.host for source in sources] == ['first.local', 'second.local']


def test_network_recs_backup_is_available_for_project_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = NetworkRecsSource(mac='aa:bb:cc:dd:ee:ff', host='pi.local')
    session = tmp_path / 'backup' / 'sources' / source.name / 'project' / 'session'
    session.mkdir(parents=True)
    (session / 'recording.toml').write_text('format = "recs"\n')
    (session / 'session-record.jsonl').write_text('{"type":"header"}\n')
    uploads: list[Path] = []

    def upload(
        path: Path,
        upload_path: Path,
        ssh_url: str,
        run: object,
    ) -> None:
        uploads.append(upload_path)

    class Discovery:
        def __init__(self) -> None:
            self.run = subprocess.run
            self.verbose = False
            self.new_machines: list[object] = []

        def discover(self) -> list[NetworkRecsSource]:
            return [source]

    monkeypatch.setattr('baccy.backup.backup_network_source', lambda *args: [])
    monkeypatch.setattr('baccy.upload._upload', upload)
    result = run_backup(
        Settings(
            backup_root=tmp_path / 'backup',
            discover_removable=False,
            projects={'project': ProjectUpload(ssh_url='user@host:/srv/recs')},
        ),
        network=cast(NetworkDiscovery, Discovery()),
    )

    assert result.uploaded == 2
    assert uploads == [
        Path('project/session/recording.toml'),
        Path('project/session/session-record.jsonl'),
    ]
