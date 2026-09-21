import logging
import subprocess
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from baccy.backup import run_backup
from baccy.models import Settings
from baccy.network import NetworkDiscovery


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

    sources = NetworkDiscovery(run, verbose=True).discover()

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
