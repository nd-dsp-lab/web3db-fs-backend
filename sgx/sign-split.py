#!/usr/bin/env python3
"""Offline (split) SGX enclave signing — the half that runs on the SGX host.

The enclave signing key must never live on the SGX host: whoever holds it can
sign an enclave that unseals every MRSIGNER-sealed secret. So signing is
split: this script measures the enclave here, the ~400-byte to-be-signed blob
travels to the machine that holds the key (plain `openssl dgst -sha256 -sign`
— SGX signatures are ordinary PKCS#1 v1.5), and the signature comes back to
be assembled into the .sig SIGSTRUCT. sign-remote.sh drives the round trip.

  prepare --name web3fs   render manifest.sgx + write web3fs.tbs / .tbs.json
  finish  --name web3fs --sig web3fs.sigbin --pubkey enclave-pub.pem
"""
import argparse
import datetime
import hashlib
import json
import sys

from cryptography.hazmat.primitives.serialization import load_pem_public_key
from graminelibos import Manifest, SGX_LIBPAL, get_tbssigstruct


def prepare(name):
    with open(f"{name}.manifest", encoding="utf-8") as f:
        manifest = Manifest.load(f)
    manifest.expand_all_trusted_files()
    with open(f"{name}.manifest.sgx", "wb") as f:
        manifest.dump(f)

    date = datetime.date.today()
    sigstruct = get_tbssigstruct(f"{name}.manifest.sgx", date, SGX_LIBPAL, verbose=False)
    data = sigstruct.get_signing_data()

    with open(f"{name}.tbs", "wb") as f:
        f.write(data)
    # finish() recomputes the sigstruct, so it must reuse this date, and the
    # digest lets it prove it rebuilt the same measurement it is signing.
    with open(f"{name}.tbs.json", "w", encoding="utf-8") as f:
        json.dump({"date": date.isoformat(), "sha256": hashlib.sha256(data).hexdigest()}, f)

    # The per-release measurement /attestation quotes carry — publish this
    # so verify-attestation.py callers have something to compare against.
    with open(f"{name}.mrenclave", "w", encoding="utf-8") as f:
        f.write(sigstruct["enclave_hash"].hex() + "\n")

    print(f"MRENCLAVE {sigstruct['enclave_hash'].hex()}")
    print(f"to sign:  {name}.tbs")


def finish(name, sig_path, pubkey_path):
    with open(f"{name}.tbs.json", encoding="utf-8") as f:
        meta = json.load(f)
    date = datetime.date.fromisoformat(meta["date"])

    sigstruct = get_tbssigstruct(f"{name}.manifest.sgx", date, SGX_LIBPAL, verbose=False)
    data = sigstruct.get_signing_data()
    if hashlib.sha256(data).hexdigest() != meta["sha256"]:
        sys.exit("signing data mismatch: manifest.sgx changed since prepare — re-run the flow")

    with open(pubkey_path, "rb") as f:
        pub = load_pem_public_key(f.read()).public_numbers()
    with open(sig_path, "rb") as f:
        signature = int.from_bytes(f.read(), byteorder="big")

    # Sigstruct.sign() verifies exponent 3 and derives the q1/q2 fields.
    sigstruct.sign(lambda _data: (pub.e, pub.n, signature))
    with open(f"{name}.sig", "wb") as f:
        f.write(sigstruct.to_bytes())

    print(f"wrote {name}.sig (MRENCLAVE {sigstruct['enclave_hash'].hex()})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--name", required=True)
    f = sub.add_parser("finish")
    f.add_argument("--name", required=True)
    f.add_argument("--sig", required=True, help="raw RSA signature from the signing host")
    f.add_argument("--pubkey", required=True, help="signing key's public half (PEM)")
    args = parser.parse_args()

    if args.cmd == "prepare":
        prepare(args.name)
    else:
        finish(args.name, args.sig, args.pubkey)


if __name__ == "__main__":
    main()
