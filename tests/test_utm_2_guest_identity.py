#!/usr/bin/env python3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.utm_2_guest_identity import (  # noqa: E402
    GuestIdentityError,
    parse_guest_identity,
    normalize_mac,
)


def main() -> None:
    raw = (
        '    "IOPlatformSerialNumber" = "SERIAL-1"\n'
        '    "IOPlatformUUID" = "UUID-1"\n'
    )
    identity = parse_guest_identity(raw, 'en0: flags=\n\tether 02:05:9e:71:aa:c8\n')
    assert identity.serial_number == "SERIAL-1"
    assert identity.platform_uuid == "UUID-1"
    assert identity.mac == "02:05:9e:71:aa:c8"
    assert normalize_mac("02-05-9E-71-AA-C8") == "02:05:9e:71:aa:c8"

    try:
        parse_guest_identity(raw + '"IOPlatformUUID" = "UUID-2"\n', "ether 02:05:9e:71:aa:c8")
    except GuestIdentityError as error:
        assert "IOPlatformUUID" in str(error)
    else:
        raise AssertionError("duplicate IOPlatformUUID was accepted")
    print("UTM_2_GUEST_IDENTITY_LOGIC=verified")


if __name__ == "__main__":
    main()
