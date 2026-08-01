# Research image for the eduLLM platform build pipeline.
#
# The platform supplies the registered base image as --build-arg BASE_IMAGE, already pinned
# to a digest. Its verify_dockerfile_base gate runs before the build and refuses this file
# unless BASE_IMAGE is declared in the global scope with no default and every stage roots at
# ${BASE_IMAGE}. The ARG is bare on purpose: a default would be a base image nobody
# registered, sitting here waiting for the day the build-arg is dropped, and the provenance
# record would still name the registered digest.
#
# Every build of a compliant file emits BuildKit's InvalidDefaultArgInFrom warning, because
# a bare ARG in a FROM is exactly what triggers it. That warning is expected and adding a
# default is the wrong response to it.
ARG BASE_IMAGE

FROM ${BASE_IMAGE}

# NOT infra/Dockerfile.validator, AND THE TWO ARE NOT THE SAME IMAGE. That file builds the
# dataset validator: it pins `FROM python:3.12-slim` by tag, and it ends in an ENTRYPOINT of
# `python -m edullm_data.validate --promote`, because it is woken by an EventBridge rule that
# carries no payload and has to discover its own work. Both are right for a container nobody
# passes arguments to and wrong for this one.
#
# A run submitted through the platform names its own command on the form, which the manifest
# carries and the job definition passes as a container override. An ENTRYPOINT here would
# prepend `python -m edullm_data.validate --promote` to it, so every submission would run the
# promoter with the submitter's command as further arguments. And the tag pin is the thing the
# registered base exists to replace: the platform records base_image_digest from
# config/repositories.yaml, so a Dockerfile choosing its own base makes that record an
# assertion about an image nothing established.
#
# The two files stay separate rather than one being made to serve both. The validator's
# image is deployed by that repository's own infrastructure against a job definition this
# platform does not own, and folding them together would put a submission path's build in
# the way of a dataset promotion.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/edullm-data

# Dependencies before the project, so the layer that takes the network is not invalidated by
# a source edit. Both are the project's own declared runtime dependencies, named again here
# with floors rather than left to `pip install .` to resolve, for the same reason
# infra/Dockerfile.validator names them: the two that matter are the ones a container cannot
# work without, and a resolution failure is cheaper in its own layer than at the end of one
# that also installed the project.
RUN python -m pip install --no-cache-dir "boto3>=1.34" "numpy>=1.26"

# The build context is the repository root and .dockerignore is what keeps .git out of this
# layer. The platform's publish job strips the checkout credentials before building, so what
# .dockerignore prevents here is weight rather than a secret -- but the ordering that makes
# that true lives in another repository, and a context that carries no .git at all does not
# depend on it.
COPY . .

# FAMILIES TRAVEL IN THE WHEEL AND THAT IS NOT A DETAIL. `families/` is at the repository
# root because it is standard content rather than code, so a path-relative lookup resolves to
# nothing once the package is installed into site-packages. pyproject.toml force-includes it
# into `edullm_data/families`, and the failure when it does not is silent: every
# family-declared bound falls back to the profile's own laxer constant, which is how a live
# corpus was once validated at 50% EOS instead of the family's 5%.
#
# So the assertion below reads `validate.FAMILIES_DIR`, which is the value production
# resolves at import time, and requires it to point inside the installed package rather than
# merely to exist. Existing is not enough here and that is the whole subtlety: the build
# context is a checkout, so the third fallback in `_resolve_families_dir` can find the root
# copy and answer correctly for the wrong reason, in an image where the directory would be
# absent the moment anything ran from another working directory.
#
# The reader is asserted too, and separately. `edullm_data.read` is what a training run in
# another repository reaches for -- OLMo-core's image installs this project by commit for
# exactly that -- so it is the module whose breakage costs the most and shows the least.
#
# Run from / rather than from the checkout, because a relative import of the source tree
# beside it would prove the checkout works and say nothing about the wheel.
RUN set -eux; \
    python -m pip install --no-cache-dir .; \
    cd /; \
    python -c "import boto3, numpy, edullm_data; print(edullm_data.__name__)"; \
    python -c "from edullm_data.read import dataset_paths, resolve_latest; print(dataset_paths.__module__)"; \
    python -c "from pathlib import Path; import edullm_data; from edullm_data import validate; packaged = Path(edullm_data.__file__).resolve().parent / 'families'; assert validate.FAMILIES_DIR == packaged, f'families resolved to {validate.FAMILIES_DIR} rather than into the wheel, so every declared bound would fall back to a laxer constant'; print(sorted(p.name for p in packaged.iterdir()))"

# No CMD and no ENTRYPOINT. The registered base's are inherited deliberately, so swapping the
# base does not leave a stale command behind, and a submission's own command is what runs.
#
# No USER either, so this image runs as root, inherited from the base. The container's blast
# radius is bounded by the workload role rather than by its uid: that role may write one
# team's output prefix and read it back, and holds nothing else.
