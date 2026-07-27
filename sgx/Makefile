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

ARCH_LIBDIR ?= /lib/$(shell $(CC) -dumpmachine)
BACKEND_ROOT ?= $(abspath ..)
VENV ?= $(BACKEND_ROOT)/venv
ENTRYPOINT ?= $(shell realpath $$(command -v python3))
HOST ?= 127.0.0.1
PORT ?= 8091
LOG_LEVEL ?= error

.PHONY: all direct clean
all: web3fs.manifest.sgx
direct: web3fs.manifest

web3fs.manifest: web3fs.manifest.template
	gramine-manifest \
		-Dlog_level=$(LOG_LEVEL) \
		-Darch_libdir=$(ARCH_LIBDIR) \
		-Dentrypoint=$(ENTRYPOINT) \
		-Dbackend_root=$(BACKEND_ROOT) \
		-Dvenv=$(VENV) \
		-Dhost=$(HOST) \
		-Dport=$(PORT) \
		$< $@

# Also emits web3fs.sig (SIGSTRUCT) — its MRENCLAVE is the measurement that
# attestation verifies against in phase 5.
web3fs.manifest.sgx: web3fs.manifest
	gramine-sgx-sign --manifest $< --output $@

clean:
	rm -f web3fs.manifest web3fs.manifest.sgx web3fs.sig
