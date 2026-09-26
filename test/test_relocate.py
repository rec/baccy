from pathlib import PurePosixPath
from subprocess import CompletedProcess

import pytest
from botocore.exceptions import ClientError

from baccy.models import S3Destination, Settings
from baccy.relocate import Relocation, planned_relocations, relocate_urls
from baccy.upload import RemoteTargetPlan


def test_relocation_planning_uses_web_safe_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = S3Destination(kind='s3', bucket='archive')
    monkeypatch.setattr(
        'baccy.relocate.planned_remote_targets',
        lambda settings: [
            RemoteTargetPlan(
                destination=destination,
                legacy_target=PurePosixPath('project name/audio/one + two.flac'),
                target=PurePosixPath('project-name/audio/one+two.flac'),
            )
        ],
    )

    values = planned_relocations(Settings())

    assert [value.source_address for value in values] == [
        's3:archive/project name/audio/one + two.flac'
    ]
    assert [value.target_address for value in values] == [
        's3:archive/project-name/audio/one+two.flac'
    ]


def test_relocation_planning_rejects_web_safe_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = S3Destination(kind='s3', bucket='archive')
    monkeypatch.setattr(
        'baccy.relocate.planned_remote_targets',
        lambda settings: [
            RemoteTargetPlan(
                destination=destination,
                legacy_target=PurePosixPath('project/a b.flac'),
                target=PurePosixPath('project/a-b.flac'),
            ),
            RemoteTargetPlan(
                destination=destination,
                legacy_target=PurePosixPath('project/a^b.flac'),
                target=PurePosixPath('project/a-b.flac'),
            ),
        ],
    )

    with pytest.raises(ValueError, match='collides'):
        planned_relocations(Settings())


def test_s3_relocation_copies_then_deletes_without_downloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = S3Destination(kind='s3', bucket='archive')
    relocation = Relocation(
        destination=destination,
        source=PurePosixPath('project name/audio.flac'),
        target=PurePosixPath('project-name/audio.flac'),
    )
    calls: list[tuple[str, object]] = []

    class Client:
        def head_object(self, **kwargs: object) -> dict[str, object]:
            calls.append(('head', kwargs))
            if kwargs['Key'] == 'project name/audio.flac':
                return {}
            raise ClientError({'Error': {'Code': '404'}}, 'head_object')

        def copy_object(self, **kwargs: object) -> None:
            calls.append(('copy', kwargs))

        def delete_object(self, **kwargs: object) -> None:
            calls.append(('delete', kwargs))

    monkeypatch.setattr('baccy.relocate.s3_client', lambda destination: Client())
    messages: list[dict[str, object]] = []
    monkeypatch.setattr('baccy.relocate._log', messages.append)

    assert relocate_urls([relocation]) is True
    assert [call[0] for call in calls] == ['head', 'head', 'copy', 'delete']
    assert calls[2][1] == {
        'Bucket': 'archive',
        'Key': 'project-name/audio.flac',
        'CopySource': {'Bucket': 'archive', 'Key': 'project name/audio.flac'},
    }
    assert messages[-1] == {'ok': True}


def test_ssh_relocation_renames_on_the_remote_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relocation = Relocation.model_validate(
        {
            'destination': {'kind': 'ssh', 'url': 'user@host:/srv/site'},
            'source': 'project name/audio.flac',
            'target': 'project-name/audio.flac',
        }
    )
    commands: list[list[str]] = []
    results = iter([0, 1, 0])

    def run(command: list[str], **kwargs: object) -> CompletedProcess[bytes]:
        commands.append(command)
        return CompletedProcess(command, next(results), b'', b'')

    monkeypatch.setattr('baccy.relocate.subprocess.run', run)
    monkeypatch.setattr('baccy.relocate._log', lambda value: None)

    assert relocate_urls([relocation]) is True
    assert commands[-1][-1] == (
        "mkdir -p /srv/site/project-name && mv '/srv/site/project name/audio.flac' "
        '/srv/site/project-name/audio.flac'
    )
