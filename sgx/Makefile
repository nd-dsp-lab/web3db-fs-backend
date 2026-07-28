# Build the Gramine manifest for the Web3FS backend.
#
#   make            render + SGX-sign (needs /dev/sgx_enclave + signing key)
#   make direct     render only — enough for `gramine-direct web3fs`
#   make clean
#
# Run from this directory on the SGX host:
#   gramine-direct web3fs      # LibOS only, no enclave — fast smoke test
#   gramine-sgx web3fs         # the real thing
#
# Overridables (defaults suit a test run beside a live server on :8090):
#   BACKEND_ROOT  checkout to serve (default: parent of this dir)
#   VENV          virtualenv with the app's dependencies
#   HOST/PORT     uvicorn bind (default 127.0.0.1:8091)
#   TLS=1         serve HTTPS with the key/cert sealed at secrets/tls/

ARCH_LIBDIR ?= /lib/$(shell $(CC) -dumpmachine)
BACKEND_ROOT ?= $(abspath ..)
VENV ?= $(BACKEND_ROOT)/venv
ENTRYPOINT ?= $(shell realpath $$(command -v python3))
HOST ?= 127.0.0.1
PORT ?= 8091
LOG_LEVEL ?= error
TLS ?= 0
EXPORT ?= 0
SLOT ?= a

# Blue/green slots: identical enclaves differing only in listen port and
# thumbnail cache, so a deploy can boot the idle one before retiring the
# live one. Slot a keeps 8090 (the port the reverse tunnel has always
# forwarded); slot b uses 8092.
SLOT_A_PORT ?= 8090
SLOT_B_PORT ?= 8092

.PHONY: all slots direct clean
all: web3fs.manifest.sgx provision.manifest.sgx
slots: web3fs-a.manifest.sgx web3fs-b.manifest.sgx provision.manifest.sgx
direct: web3fs.manifest

# The server enclave and — rendered from the same template with provision=1 —
# a one-shot enclave that seals a plaintext .env from stdin into the
# encrypted mount:  cat .env | gramine-sgx provision
web3fs.manifest: PROVISION = 0
provision.manifest: PROVISION = 1
export.manifest: PROVISION = 0
export.manifest: EXPORT = 1
web3fs-a.manifest: PROVISION = 0
web3fs-a.manifest: SLOT = a
web3fs-a.manifest: PORT = $(SLOT_A_PORT)
web3fs-b.manifest: PROVISION = 0
web3fs-b.manifest: SLOT = b
web3fs-b.manifest: PORT = $(SLOT_B_PORT)
web3fs.manifest provision.manifest export.manifest web3fs-a.manifest web3fs-b.manifest: web3fs.manifest.template
	gramine-manifest \
		-Dlog_level=$(LOG_LEVEL) \
		-Darch_libdir=$(ARCH_LIBDIR) \
		-Dentrypoint=$(ENTRYPOINT) \
		-Dbackend_root=$(BACKEND_ROOT) \
		-Dvenv=$(VENV) \
		-Dhost=$(HOST) \
		-Dport=$(PORT) \
		-Dprovision=$(PROVISION) \
		-Dtls=$(TLS) \
		-Dexport=$(EXPORT) \
		-Dslot=$(SLOT) \
		$< $@

# Also emits the .sig (SIGSTRUCT) — its MRENCLAVE is the measurement that
# attestation verifies against in phase 5. Both enclaves carry the same
# MRSIGNER, which is what lets the provision tool write what the server
# later unseals.
%.manifest.sgx: %.manifest
	gramine-sgx-sign --manifest $< --output $@

clean:
	rm -f *.manifest *.manifest.sgx *.sig *.mrenclave
