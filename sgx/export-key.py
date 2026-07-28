"""One-shot enclave entrypoint: print the file-content master key to stdout.

The master key is generated inside the enclave and never leaves it, which
makes it a single point of loss: it is sealed to the platform, so a CPU
swap, a board repair, a BIOS change that bumps the SGX owner epoch, or the
machine dying takes every encrypted file with it. This exists so a copy can
be kept off the machine:

    ssh <sgx-host> 'cd <sgx-dir> && gramine-sgx export-key' > master-key.hex

Note what this tool is: anything that can run it can extract the key. Sign
it, use it, then delete export.manifest.sgx and export.sig from the host —
without them the enclave cannot launch, and re-signing needs the signing
key, which does not live on the SGX host.

stdout carries the key alone (nothing else, so a redirect captures exactly
it); the digest for verification goes to stderr.
"""
import hashlib
import sys

KEY_FILE = "/web3fs/secrets/file_master_key"


def main():
    with open(KEY_FILE, encoding="utf-8") as f:
        key_hex = f.read().strip()

    try:
        raw = bytes.fromhex(key_hex)
    except ValueError:
        sys.exit("master key is not hex — refusing to export something unexpected")
    if len(raw) != 32:
        sys.exit(f"expected a 32-byte master key, found {len(raw)} bytes")

    sys.stdout.write(key_hex)
    print(f"exported {len(raw)}-byte key, sha256(hex-text) = "
          f"{hashlib.sha256(key_hex.encode()).hexdigest()}", file=sys.stderr)


if __name__ == "__main__":
    main()
