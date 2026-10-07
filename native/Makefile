# Makefile to build the pw_bridge C wrapper.
#
# The wrapper is a shared library that will be loaded by CPython
# via ctypes. It depends on:
#   - libpipewire-0.3
#   - Python C headers (for PyGILState_Ensure / PyGILState_Release)
#
# The Python headers are located automatically via sysconfig.

CC      ?= cc
CFLAGS  ?= -O2 -fPIC -Wall -Wextra
LDFLAGS ?= -shared

PKG_CONFIG ?= pkg-config
PYTHON     ?= python3

PW_CFLAGS  := $(shell $(PKG_CONFIG) --cflags libpipewire-0.3)
PW_LIBS    := $(shell $(PKG_CONFIG) --libs libpipewire-0.3)

PY_CFLAGS  := $(shell $(PYTHON) -c "import sysconfig; print('-I' + sysconfig.get_paths()['include'])")

TARGET := libpw_bridge.so
SRC    := pw_bridge.c
OBJ    := $(SRC:.c=.o)

.PHONY: all clean rebuild check-deps

all: check-deps $(TARGET)

check-deps:
	@$(PKG_CONFIG) --exists libpipewire-0.3 || \
		(echo "Error: libpipewire-0.3 not found."; \
		 echo "Install libpipewire-0.3-dev (Debian/Ubuntu) or the equivalent."; \
		 exit 1)
	@$(PYTHON) -c "import sysconfig" || \
		(echo "Error: python3 sysconfig not available."; exit 1)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) $(PW_CFLAGS) $(PY_CFLAGS) $(LDFLAGS) -o $@ $< $(PW_LIBS)

clean:
	rm -f $(TARGET) $(OBJ)

rebuild:
	$(MAKE) clean
	$(MAKE) all