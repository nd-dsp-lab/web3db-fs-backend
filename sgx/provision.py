"""One-shot enclave entrypoint: unpack a tar from stdin into /web3fs/secrets.

This is how plaintext secrets get INTO the sealed mount — piped over SSH
into an enclave signed with our key, never touching the host disk:

    tar -C <dir-with-secrets> -cf - . | ssh <sgx-host> \
        'cd <sgx-dir> && gramine-sgx provision'

Files land encrypted under the MRSIGNER sealing key. Existing files are
unlinked before extraction: rewriting an encrypted file verifies the old
content first, so a corrupted (or tampered-with) copy would EACCES instead
of being replaced.
"""
import os
import sys
import tarfile

DEST = "/web3fs/secrets"


def main():
    count = 0
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as tar:
        for member in tar:
            if not member.isfile():
                continue
            # normpath, NOT lstrip("./") — lstrip strips a char set, which
            # would rename ".env" to "env".
            name = os.path.normpath(member.name)
            # Refuse anything that would escape the sealed mount.
            if name.startswith("/") or ".." in name.split("/"):
                sys.exit(f"refusing suspicious path in tar: {member.name!r}")

            path = os.path.join(DEST, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                os.remove(path)
            src = tar.extractfile(member)
            with open(path, "wb") as dst:
                dst.write(src.read())
            count += 1

    print(f"sealed {count} file(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
