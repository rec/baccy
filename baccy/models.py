from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .match import validate_match


class PathSource(BaseModel, frozen=True):
    kind: Literal['path']
    name: str
    path: Path
    include: list[str] = Field(default_factory=lambda: ['**'])
    exclude: list[str] = Field(default_factory=list)

    @field_validator('name')
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value or '/' in value or value in {'.', '..'}:
            raise ValueError('source name must be a non-empty path component')
        return value


class VolumeSource(BaseModel, frozen=True):
    kind: Literal['volume']
    name: str
    uuid: str
    relative_path: Path = Path('.')
    expected_name: str | None = None
    include: list[str] = Field(default_factory=lambda: ['**'])
    exclude: list[str] = Field(default_factory=list)

    @field_validator('name')
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value or '/' in value or value in {'.', '..'}:
            raise ValueError('source name must be a non-empty path component')
        return value

    @field_validator('uuid')
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        if not value:
            raise ValueError('volume UUID must not be empty')
        return value


class NetworkSource(BaseModel, frozen=True):
    kind: Literal['network']
    name: str
    mac: str
    host: str
    include: list[str] = Field(default_factory=lambda: ['**'])
    exclude: list[str] = Field(default_factory=list)


Source = PathSource | VolumeSource | NetworkSource


class SshDestination(BaseModel, frozen=True):
    kind: Literal['ssh']
    url: str

    @field_validator('url')
    @classmethod
    def validate_ssh_url(cls, value: str) -> str:
        host, separator, path = value.partition(':')
        if not host or not separator or not path or ' ' in value:
            raise ValueError('SSH URL must be HOST:PATH without spaces')
        if PurePosixPath(path).is_absolute() is False:
            raise ValueError('SSH URL path must be absolute')
        return value

    model_config = {'extra': 'forbid'}


class S3Destination(BaseModel, frozen=True):
    kind: Literal['s3']
    bucket: str
    endpoint_url: str | None = None
    prefix: str = ''

    @field_validator('bucket')
    @classmethod
    def validate_bucket(cls, value: str) -> str:
        if not value or '/' in value:
            raise ValueError('S3 bucket must be a non-empty bucket name')
        return value

    @field_validator('prefix')
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('S3 prefix must be a relative path')
        return value.strip('/')

    model_config = {'extra': 'forbid'}


Destination = Annotated[SshDestination | S3Destination, Field(discriminator='kind')]


class Encoding(BaseModel, frozen=True):
    format: Literal['source', 'flac', 'mp3']
    bitrate_kbps: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def validate_bitrate(self) -> Encoding:
        if self.format == 'mp3' and self.bitrate_kbps is None:
            raise ValueError('MP3 encoding requires bitrate_kbps')
        if self.format != 'mp3' and self.bitrate_kbps is not None:
            raise ValueError('only MP3 encoding accepts bitrate_kbps')
        return self

    model_config = {'extra': 'forbid'}


class UploadRule(BaseModel, frozen=True):
    name: str
    match: str
    encoding: Encoding
    destination: str

    @field_validator('name', 'destination')
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not value or '/' in value or value in {'.', '..'}:
            raise ValueError(
                'upload rule names and destinations must be path components'
            )
        return value

    @field_validator('match')
    @classmethod
    def validate_match_expression(cls, value: str) -> str:
        return validate_match(value)

    model_config = {'extra': 'forbid'}


class Settings(BaseModel, frozen=True):
    backup_root: Path = Field(default_factory=lambda: Path.home() / 'baccy')
    sources: list[Source] = Field(default_factory=list)
    discover_removable: bool = True
    poll_seconds: float = Field(default=60.0, gt=0)
    stability_seconds: float = Field(default=60.0, ge=0)
    verbose: bool = True
    destinations: dict[str, Destination] = Field(default_factory=dict)
    uploads: list[UploadRule] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_source_names(self) -> Settings:
        names = [s.name for s in self.sources]
        if len(names) != len(set(names)):
            raise ValueError('source names must be unique')
        destination_names = set(self.destinations)
        names = [r.name for r in self.uploads]
        if len(names) != len(set(names)):
            raise ValueError('upload rule names must be unique')
        for rule in self.uploads:
            if rule.destination not in destination_names:
                raise ValueError(f'unknown upload destination: {rule.destination}')
        return self

    model_config = {'extra': 'forbid'}


class SourceSelection(BaseModel, frozen=True):
    relative_root: Path = Path('.')
    extensions: list[str] | None = None

    @field_validator('relative_root')
    @classmethod
    def validate_relative_root(cls, value: Path) -> Path:
        if value.is_absolute() or '..' in value.parts:
            raise ValueError('source selection root must be relative')
        return value


class ResolvedSource(BaseModel, frozen=True):
    source: Source
    root: Path
    selections: list[SourceSelection] = Field(
        default_factory=lambda: [SourceSelection()]
    )


class Candidate(BaseModel, frozen=True):
    source: ResolvedSource
    path: Path
    relative_path: Path
    priority: int
    active: bool = False
    project: str | None = None


class FileResult(BaseModel, frozen=True):
    source: str
    relative_path: Path | None = None
    status: str
    detail: str | None = None


class RecognizedSource(BaseModel, frozen=True):
    source: str
    label: str
    kind: Literal['disk', 'machine']


class BackupSummary(BaseModel, frozen=True):
    discovered: int = 0
    would_copy: int = 0
    would_upload: int = 0
    copied: int = 0
    uploaded: int = 0
    unchanged: int = 0
    deferred: int = 0
    unavailable: int = 0
    failed: int = 0
    results: list[FileResult] = Field(default_factory=list)

    def with_result(self, result: FileResult) -> BackupSummary:
        values = self.model_dump()
        values[result.status] += 1
        values['results'].append(result)
        return BackupSummary.model_validate(values)
