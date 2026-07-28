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
CSR ?= 0
PROMOTE ?= 0

.PHONY: all direct clean
all: web3fs.manifest.sgx provision.manifest.sgx
direct: web3fs.manifest

# The server enclave and — rendered from the same template with provision=1 —
# a one-shot enclave that seals a plaintext .env from stdin into the
# encrypted mount:  cat .env | gramine-sgx provision
web3fs.manifest: PROVISION = 0
provision.manifest: PROVISION = 1
export.manifest: PROVISION = 0
export.manifest: EXPORT = 1
csr.manifest: PROVISION = 0
csr.manifest: CSR = 1
promote.manifest: PROVISION = 0
promote.manifest: PROMOTE = 1
web3fs.manifest provision.manifest export.manifest csr.manifest promote.manifest: web3fs.manifest.template
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
		-Dcsr=$(CSR) \
		-Dpromote=$(PROMOTE) \
		$< $@

# Also emits the .sig (SIGSTRUCT) — its MRENCLAVE is the measurement that
# attestation verifies against in phase 5. Both enclaves carry the same
# MRSIGNER, which is what lets the provision tool write what the server
# later unseals.
%.manifest.sgx: %.manifest
	gramine-sgx-sign --manifest $< --output $@

clean:
	rm -f web3fs.manifest provision.manifest export.manifest *.manifest.sgx *.sig *.mrenclave
