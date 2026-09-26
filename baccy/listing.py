from .catalog import Catalog
from .models import (
    Destination,
    Settings,
    SshDestination,
    parse_destination,
)
from .s3 import s3_endpoint_url


def list_uploaded(settings: Settings) -> list[str]:
    destinations = {
        _destination_identity(destination): _display_destination(destination)
        for destination in _destinations(settings)
    }
    paths = {
        f'{destination}/{target}'
        for event in Catalog(settings.backup_root).uploaded()
        if isinstance(identity := event.get('destination'), str)
        and isinstance(target := event.get('target'), str)
        and (destination := destinations.get(identity)) is not None
    }
    return sorted(paths)


def _destinations(settings: Settings) -> list[Destination]:
    values = [rule.destination for rule in settings.uploads]
    values.extend(page.destination for page in settings.landing_pages)
    return [
        parse_destination(value, settings.s3_max_bandwidth)
        for value in dict.fromkeys(values)
    ]


def _destination_identity(destination: Destination) -> str:
    if isinstance(destination, SshDestination):
        return destination.url
    endpoint = s3_endpoint_url(destination) or 'aws'
    return f'{endpoint}/{destination.bucket}/{destination.prefix}'


def _display_destination(destination: Destination) -> str:
    if isinstance(destination, SshDestination):
        return f'ssh:{destination.url}'
    return f's3:{destination.bucket}'
