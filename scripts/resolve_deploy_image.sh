#!/bin/bash
# Resolve, and REFUSE, the image a production deploy is allowed to run.
#
# This is the whole of ERP's image-mutability gate. It exists as its own file,
# rather than as a function inside scripts/deploy.sh, for one reason: deploy.sh
# cannot be sourced without running a deployment, so a gate living inside it
# could never be driven by a test. A guard nothing can exercise is an assertion
# about a guard, not a guard — see tests/architecture/test_deploy_image_gate.py,
# which drives THIS script in both directions.
#
# The defect it closes. `docker-compose.yml` used to read
# `ghcr.io/michaelayoade/dotmac_erp:${ERP_IMAGE_TAG:-latest}` and deploy.sh
# pinned that variable to `sha-$(git rev-parse --short=7 HEAD)`. A `sha-<short>`
# tag is reproducible-LOOKING and mutable in fact: it is a registry pointer that
# can be repushed at any time, and nothing bound it to the bytes CI actually
# tested. Meanwhile ERP's own publish lane already resolved the OCI digest of
# the tested image from `imagetools inspect --raw | sha256sum` and recorded it
# in `image-release.json` and in `deploy/product.toml`. Production consumed the
# tag; the pipeline verified the digest. This script makes the digest the only
# thing production can consume.
#
# Usage:
#   resolve_deploy_image.sh --compose <rendered-compose-file>
#       Read the product image reference out of a rendered Compose project and
#       print it. Every reference to $IMAGE_REPOSITORY in that file must be the
#       same immutable digest; a tag anywhere is a refusal for the whole file.
#
#   resolve_deploy_image.sh --reference <ref>
#       Validate one explicit reference and print it. Accepts a bare
#       `sha256:<64 hex>` (expanded against $IMAGE_REPOSITORY) or a full
#       `<repository>@sha256:<64 hex>`.
#
# On success the immutable reference is printed on stdout and nothing else is;
# on refusal the reason goes to stderr and the exit status is non-zero. Callers
# capture stdout, so every diagnostic here must stay on stderr.

set -euo pipefail

# Configurable, with a documented default, like every other address in this
# repository ("Everything by config"). It shares the name deploy.sh already
# uses for image retention so a fork or a mirror is ONE knob, not two.
IMAGE_REPOSITORY="${DOCKER_IMAGE_REPOSITORY:-ghcr.io/michaelayoade/dotmac_erp}"

usage() {
    echo "usage: resolve_deploy_image.sh --compose <file> | --reference <ref>" >&2
    exit 2
}

# THE gate. Every path through this script funnels through this one function,
# so there is exactly one place that decides what "immutable" means and exactly
# one regular expression to review. `^sha256:[0-9a-f]{64}$` is the same shape
# dotmac_sub's production release gate requires, and the same shape ERP's own
# publish lane already asserts on the digest it resolves (.github/workflows/
# ci.yml, "Resolve the registry digest of the tested image").
require_immutable_reference() {
    local reference="$1" origin="$2" digest

    digest="${reference#"${IMAGE_REPOSITORY}"@}"
    if [[ "$digest" == "$reference" ]] || \
       [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "IMAGE INTEGRITY FAILURE: ${origin} names" >&2
        echo "  ${reference}" >&2
        echo "which is not ${IMAGE_REPOSITORY}@sha256:<64 hex>." >&2
        echo "" >&2
        echo "A production deploy consumes an immutable OCI digest. A tag —" >&2
        echo "including a 'reproducible-looking' sha-<short> one — is a registry" >&2
        echo "pointer that can be repushed after it was verified, so it does not" >&2
        echo "identify the bytes CI tested. Re-render deploy/rendered from" >&2
        echo "deploy/product.toml, or pass an exact digest." >&2
        return 1
    fi
    return 0
}

# Every `image:` value in a Compose file, unquoted and trimmed.
#
# Parsed line-wise with shell builtins on purpose: this runs on a deployment
# host before any container starts, so it may not assume python3, yq or a
# YAML library is installed. The rendered file is machine-generated with one
# `image:` per service at a fixed shape, which is what makes the simple parse
# sound rather than merely convenient.
compose_image_references() {
    local file="$1" line reference
    while IFS= read -r line; do
        reference="${line#*image:}"
        reference="${reference#"${reference%%[![:space:]]*}"}"
        reference="${reference%"${reference##*[![:space:]]}"}"
        reference="${reference%\"}"; reference="${reference#\"}"
        reference="${reference%\'}"; reference="${reference#\'}"
        # `if`, not `[[ ... ]] && ...`: under `set -e` a failing AND-list in
        # tail position aborts the shell, so the guard would turn a blank
        # value into a silent exit instead of a skipped line.
        if [[ -n "$reference" ]]; then
            printf '%s\n' "$reference"
        fi
    done < <(grep -E '^[[:space:]]*image:[[:space:]]*[^[:space:]]' "$file" || true)
}

resolve_from_compose() {
    local file="$1" reference resolved="" product_references=0

    if [[ ! -r "$file" ]]; then
        echo "IMAGE INTEGRITY FAILURE: ${file} is not readable." >&2
        return 1
    fi

    while IFS= read -r reference; do
        # Only this product's own image is in scope. A dependency image
        # (`redis:7`) is legitimately tag-named and is not what a deploy of ERP
        # is pinning; refusing it would make the gate unusable and would teach
        # the next person to switch it off.
        case "$reference" in
            "${IMAGE_REPOSITORY}"|"${IMAGE_REPOSITORY}"@*|"${IMAGE_REPOSITORY}":*) ;;
            *) continue ;;
        esac
        product_references=$((product_references + 1))
        require_immutable_reference "$reference" "${file}" || return 1
        if [[ -z "$resolved" ]]; then
            resolved="$reference"
        elif [[ "$resolved" != "$reference" ]]; then
            # Two different digests in one project means app, worker and beat
            # would not be one release. The old ERP_IMAGE_TAG path made this
            # structurally impossible by sharing a variable; naming the digest
            # per service gives it back, so it is checked rather than assumed.
            echo "IMAGE INTEGRITY FAILURE: ${file} names more than one" >&2
            echo "  ${IMAGE_REPOSITORY} image: ${resolved} and ${reference}." >&2
            echo "Every role in one deploy must run one release." >&2
            return 1
        fi
    done < <(compose_image_references "$file")

    if ((product_references == 0)); then
        # Absence must fail. If it returned empty and the caller carried on, a
        # renamed repository or a reshaped rendered file would silently produce
        # "no image" and this gate would assert nothing at all.
        echo "IMAGE INTEGRITY FAILURE: ${file} names no ${IMAGE_REPOSITORY}" >&2
        echo "image at all, so there is nothing to deploy and nothing to check." >&2
        return 1
    fi

    printf '%s\n' "$resolved"
}

resolve_from_reference() {
    local selector="$1" reference="$1"

    # A bare digest is the operator-facing spelling (`deploy.sh sha256:...`),
    # matching dotmac_sub. It is expanded here and then held to the identical
    # gate as anything read from a file — one selector shape, one check.
    if [[ "$selector" == sha256:* ]]; then
        reference="${IMAGE_REPOSITORY}@${selector}"
    fi
    require_immutable_reference "$reference" "the requested image selector" || return 1
    printf '%s\n' "$reference"
}

[[ $# -eq 2 ]] || usage
case "$1" in
    --compose)   resolve_from_compose "$2" ;;
    --reference) resolve_from_reference "$2" ;;
    *)           usage ;;
esac
