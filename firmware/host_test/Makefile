# Builds the firmware's pure-C components with a host compiler and checks
# them against golden vectors generated from the Python.
#
#     make          regenerate the vectors, build, run
#     make run      build and run without regenerating
#     make check    fail if the checked-in vectors are stale
#
# Nothing here needs ESP-IDF: gc_contract, gc_oref, gc_predict and gc_store
# are ordinary C99 with no dependency on the SDK, which is exactly why the
# forecast lives in them rather than in the display code.

CC ?= cc
PYTHON ?= python3

ROOT := $(CURDIR)/../..
COMPONENTS := $(ROOT)/firmware/components

CFLAGS ?= -std=c11 -O2 -g \
	-Wall -Wextra -Werror \
	-Wno-unused-parameter \
	-fno-fast-math

INCLUDES := \
	-I$(COMPONENTS)/gc_contract/include \
	-I$(COMPONENTS)/gc_oref/include \
	-I$(COMPONENTS)/gc_predict/include \
	-I$(COMPONENTS)/gc_store/include \
	-I$(CURDIR)

SOURCES := \
	$(COMPONENTS)/gc_contract/gc_contract.c \
	$(COMPONENTS)/gc_oref/gc_oref.c \
	$(COMPONENTS)/gc_predict/gc_predict.c \
	$(COMPONENTS)/gc_store/gc_store.c \
	$(CURDIR)/vectors.c \
	$(CURDIR)/test_parity.c

BINARY := $(CURDIR)/test_parity

.PHONY: all run check vectors clean

all: vectors run

vectors:
	$(PYTHON) $(ROOT)/firmware/host_test/gen_vectors.py

# Used by CI: the checked-in vectors must already match the Python, so a
# forecast change that was not regenerated fails here rather than shipping.
check:
	$(PYTHON) $(ROOT)/firmware/tools/gen_contract.py --check
	$(PYTHON) $(ROOT)/firmware/host_test/gen_vectors.py --check
	$(MAKE) run

run: $(BINARY)
	$(BINARY)

$(BINARY): $(SOURCES) $(wildcard $(CURDIR)/*.h)
	$(CC) $(CFLAGS) $(INCLUDES) $(SOURCES) -lm -o $@

clean:
	rm -f $(BINARY)
