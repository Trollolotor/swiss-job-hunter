"""Product bridge for ready SDK libraries; never executes a connector in the backend."""

import os

from ats_core import SchemaError
from ats_vendors.registry import from_config, manifest, network_rules, wrapper
from employers.config import get_config


def ready_candidate(source):
    from employers.service import candidate

    try:
        portal = from_config(source["platform"], source["config"])
    except SchemaError:
        return None
    recipe = manifest(portal)
    recipe["catalogue"]["max_pages"] = get_config().max_pages
    config = {
        **portal.config(),
        "max_pages": get_config().max_pages,
        "library_fingerprint": recipe["ats_library"]["fingerprint"],
    }
    return candidate(
        source["id"],
        code=wrapper(portal.vendor),
        manifest=recipe,
        config=config,
        parent_id=source.get("active_version_id"),
        image_digest=os.environ.get("EMPLOYER_RUNNER_IMAGE", ""),
        policy=network_rules(portal),
    )
