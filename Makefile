################################################################################
#                                                                              #
#                            M A K E F I L E                                   #
#                                                                              #
################################################################################

################################################################################
#
#  Description: This is just a convenience makefile that points to the actual
#               makefile and executes.
#
# Directory Structure:
#    /CaptionInspector/
#     |
#     +---/include/{All Include Files}
#     |
#     +---/obj/{All Generated Object and Dependency Files}
#     |
#     +---/python/{Python Code}
#     |    |
#     |    +-----/libci.dylib (Generated Dynamic Library for use with Python)
#     |    |
#     |    +-----/Makefile (Convenience Makefile that just points to this makefile)
#     |
#     +---/src/
#     |    |
#     |    +--/source/{Source Files for the Sources}
#     |    |
#     |    +--/xform/{Source Files for the Transforms}
#     |    |
#     |    +--/sink/{Source Files for the Sinks}
#     |    |
#     |    +--/utils/{Source Files for the Utilities}
#     |    |
#     |    +--/makefile  (Actual Makefile)
#     |
#     +---/test/{C Test Code}
#     |    |
#     |    +-----/libci-test.dylib (Generated Dynamic Test Library for use with Python Tests)
#     |    |
#     |    +-----/python/{Python Test Code}
#     |    |
#     |    +-----/Makefile  (Test Makefile)
#     |
#     +---/makefile (Convenience Makefile that just points to the actual makefile)
#     |    ^^^^^^^
#     |   YOU ARE HERE!
#     |
#     +---/caption-inspector  (Generated Binary Executable)
#
################################################################################

ifeq (run,$(firstword $(MAKECMDGOALS)))
  # use the rest as arguments for "run"
  RUN_ARGS := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
  # ...and turn them into do-nothing targets
  $(eval $(RUN_ARGS):;@:)
endif

.PHONY: all sharedlib app app-check web-app sync-app offline-app verify-offline-app dist stage-model clean build docker example

all:
	cd src ; make all

sharedlib:
	cd src ; make sharedlib

app:
	python3 python/launch_app.py

app-check:
	python3 python/launch_app.py --check

web-app:
	python3 python/launch_app.py --web

# Caption Inspector v6.0.app carries its own copy of python/ so it can be copied
# out of this working tree and still run. Re-run this after editing python/ to
# refresh the bundled copy.
sync-app:
	rsync -a --exclude '__pycache__' --exclude 'venv' --exclude '.venv' --exclude 'jobs' \
		python/ "Caption Inspector v7.5.app/Contents/Resources/python/"

# Fully self-contained bundle: its own Python, ffmpeg, wheels and model weights.
# Needs a framework Python (python.org) and one online pip step to build; the
# result never touches the network. See packaging/README.md.
#
# MODELS picks which transcription models travel with it, e.g.
#   make offline-app MODELS=base,medium
# Every model the app offers must be bundled, because a built app will not
# download one. Sizes: tiny 78 MB, base 148 MB, small 500 MB, medium 1.5 GB,
# large-v3 2.9 GB.
MODELS ?= base

offline-app: sharedlib
	python3 packaging/build_offline_app.py --models $(MODELS)

verify-offline-app:
	python3 packaging/verify_offline_app.py "Caption Inspector v7.5.app"

# Add a model to an already-built bundle, without rebuilding it.
#   make stage-model MODEL=medium
stage-model:
	python3 packaging/stage_model.py --model $(MODEL) --download "Caption Inspector v7.5.app"

# Verify, then package into a DMG and a zip with checksums, in packaging/dist/.
# Add SIGN_IDENTITY=... NOTARY_PROFILE=... to produce something that opens
# without the recipient touching Terminal. See packaging/README.md.
dist:
	python3 packaging/make_distributable.py "Caption Inspector v7.5.app" \
		$(if $(SIGN_IDENTITY),--identity "$(SIGN_IDENTITY)") \
		$(if $(NOTARY_PROFILE),--notarize-profile "$(NOTARY_PROFILE)")

caption-inspector:
	cd src ; make ../caption-inspector

ci_without_ffmpeg:
	cd src ; make ci_without_ffmpeg

ci_with_gpac:
	cd src ; make ci_with_gpac

docker:
	docker build -t caption-inspector .

docker-test:
	cd test ; make docker

clean:
	cd src ; make clean

VERSION_CLEANUP=make version-cleanup || { make version-cleanup; exit 1; }

GIT_VERSION = $(shell if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git describe --match "v[0-9]*" --always --long | sed -e "s/-.*//"; else echo v0.0; fi)
GIT_BUILD = $(shell if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git describe --match "v[0-9]*" --always --long; else echo v0.0-local; fi)
GIT_COMMIT = $(shell if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git describe --match "v[0-9]*" --always --long | sed -e "s/.*-g//"; else echo local; fi)
GIT_COMMIT_LONG = $(shell if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git describe --match "v[0-9]*" --long --abbrev=40 | sed -e "s/.*-g//"; else echo local; fi)
define VERSION_BODY
// this file generated by build process
const char* VERSION = "${GIT_VERSION}";
const char* BUILD = "${GIT_BUILD}";
const char* COMMIT = "${GIT_COMMIT}";
const char* COMMIT_LONG = "${GIT_COMMIT_LONG}";
endef
export VERSION_BODY

.PHONY: version version-cleanup src/utils/version.c
version: src/utils/version.c
src/utils/version.c:
	@echo "Building Version: ${GIT_VERSION} (${GIT_BUILD})"
	git describe --match "v[0-9]*" --always --long
	@echo "$$VERSION_BODY" > $@
version-cleanup:
	git checkout -- src/utils/version.c
