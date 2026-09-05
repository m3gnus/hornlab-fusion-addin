"""Author WG source roles and body declarations, and preview a return.

Deliberately free of ``adsk``, exactly like :mod:`wglink_watch`: every Fusion
API call has to happen on Fusion's main thread inside a command handler, so
WGLink.py owns the dialogs, the live entities and every write, while this
module owns the decisions -- which appearance name a role means, which of the
selected faces actually change, and what the Send dialog says before anything
is exported.  That split is what makes the authoring commands testable without
Fusion.

The two conventions this module encodes are not invented here:

* a source is a face whose *appearance name* is exactly ``LF``, ``MF``, ``HF``
  or ``PASSIVE_CARDIOID`` (``wglink_send._face_role`` upper-cases and matches
  it against ``RECOGNISED_SOURCE_ROLES``, which also accepts the retired
  ``PORT_EXIT`` spelling on faces painted before the rename), and
* an unlinked ("Fusion-first") return is solved in the assembly frame itself:
  radiation along +Z, throat at z = 0, model centred on x = 0 and y = 0.  The
  solver hard-codes that identity frame; see ``assembly_frame_is_solver_frame``
  in the generator's ``server/solver/metal.py``.  Nothing here changes it -- the
  pre-flight only reports how far the model sits from it.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import math
from typing import Any, Mapping, Sequence


class AuthorError(Exception):
    """A refusal the user can act on. WGLink.py shows it like a WgLinkError.

    Deliberately not ``wglink_core.WgLinkError``: that module imports ``adsk``,
    and this one has to stay importable -- and testable -- without Fusion.
    """


# ---------------------------------------------------------------- source roles

SOURCE_ROLES = ("LF", "MF", "HF", "PASSIVE_CARDIOID")
DEFAULT_SOURCE_ROLE = "HF"
CLEAR_SOURCE_LABEL = "Clear WG source"

# ``PORT_EXIT`` was the original name for the passive-cardioid port role.
# Choosing or typing it still has to work -- and still has to land on the new
# canonical role -- so existing habits and any saved dropdown selection do not
# start erroring.
LEGACY_SOURCE_ROLE_ALIASES = {"PORT_EXIT": "PASSIVE_CARDIOID"}

# Every appearance name that counts as an already-painted source: the current
# roles plus every retired spelling. Recognition (a face someone already
# painted) has to accept this wider set; painting a *new* role only ever
# offers ``SOURCE_ROLES``. Keeping the two separate is what lets a face
# painted ``PORT_EXIT`` years ago keep reporting ``PORT_EXIT`` -- recognised,
# never silently renamed.
RECOGNISED_SOURCE_ROLES = SOURCE_ROLES + tuple(LEGACY_SOURCE_ROLE_ALIASES)

_ROLE_MEANING = {
    "LF": "LF drive",
    "MF": "MF drive",
    "HF": "HF drive",
    "PASSIVE_CARDIOID": "passive cardioid port",
}


def source_choices() -> tuple[str, ...]:
    """The dropdown items of the Set WG Source command, in display order."""

    return (*SOURCE_ROLES, CLEAR_SOURCE_LABEL)


def resolve_source_choice(choice: object) -> str | None:
    """Turn a dropdown item into a role, or ``None`` for "clear"."""

    value = str(choice if choice is not None else "").strip()
    if not value:
        raise AuthorError(
            f"Choose a WG source role, or {CLEAR_SOURCE_LABEL!r}."
        )
    if value.casefold() == CLEAR_SOURCE_LABEL.casefold():
        return None
    canonical = value.upper().replace(" ", "_").replace("-", "_")
    canonical = LEGACY_SOURCE_ROLE_ALIASES.get(canonical, canonical)
    if canonical not in SOURCE_ROLES:
        raise AuthorError(
            f"{value!r} is not a WG source role. Choose one of "
            f"{', '.join(SOURCE_ROLES)}, or {CLEAR_SOURCE_LABEL!r}."
        )
    return canonical


def role_appearance_name(role: object) -> str:
    """The exact Fusion appearance name a role has to be authored as.

    The appearance name *is* the convention: the export reads
    ``face.appearance.name``, upper-cases it, and accepts it only when it is
    one of the four roles.  A near miss like "HF drive" is silently not a
    source, which is the failure this command exists to prevent.
    """

    value = resolve_source_choice(role)
    if value is None:
        raise AuthorError(f"{CLEAR_SOURCE_LABEL!r} has no appearance name.")
    return value


def source_help_text(choice: object) -> str:
    """One line of dialog copy that teaches the convention while choosing."""

    role = resolve_source_choice(choice)
    if role is None:
        return (
            "Removes the WG source role from the selected faces: they go back to "
            "their body's own appearance and stop driving WG solves."
        )
    return (
        f"Marks the selected face as the {_ROLE_MEANING[role]} source for WG "
        f"solves, by painting it with an appearance named {role!r}."
    )


def _canonical_role(value: object) -> str | None:
    """The role a face already carries, or ``None`` for any other appearance.

    Deliberately returns the *literal* painted name, never an alias-resolved
    one: a face already painted ``PORT_EXIT`` is recognised as carrying a
    role, but this never rewrites it to ``PASSIVE_CARDIOID`` on its own --
    that only happens when the user explicitly repaints it.
    """

    if not isinstance(value, str):
        return None
    canonical = value.strip().upper()
    return canonical if canonical in RECOGNISED_SOURCE_ROLES else None


@dataclass(frozen=True)
class SourcePlan:
    """Which of the selected faces the command paints, clears, and leaves."""

    role: str | None
    appearance_name: str | None
    paint: tuple[int, ...]
    clear: tuple[int, ...]
    unchanged: tuple[int, ...]
    summary: str


def _indices(entries: Sequence[Mapping[str, Any]], noun: str) -> list[int]:
    result = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise AuthorError(f"Could not read the selected {noun}.")
        try:
            result.append(int(entry.get("index", position)))
        except (TypeError, ValueError) as exc:
            raise AuthorError(f"Could not read the selected {noun}.") from exc
    return result


def _total_area_mm2(
    entries: Sequence[Mapping[str, Any]], indices: set[int], positions: list[int]
) -> float | None:
    total = 0.0
    for position, entry in enumerate(entries):
        if positions[position] not in indices:
            continue
        value = entry.get("area_mm2")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        if not math.isfinite(float(value)):
            return None
        total += float(value)
    return total


def _faces_phrase(count: int) -> str:
    return "1 face" if count == 1 else f"{count} faces"


def _was(count: int) -> str:
    return "was" if count == 1 else "were"


def plan_source_assignment(
    faces: Sequence[Mapping[str, Any]], choice: object
) -> SourcePlan:
    """Decide what happens to each selected face.

    Painting replaces whatever role a face already carried, so switching a face
    from MF to HF is one action rather than clear-then-paint.  Clearing is
    deliberately narrower: only a face that actually carries one of the four
    roles is stripped, so a Clear that swept up an ordinary painted face never
    destroys the user's own material assignment.
    """

    role = resolve_source_choice(choice)
    entries = list(faces)
    if not entries:
        raise AuthorError(
            "Select at least one face. Faces are marked by painting them with a "
            "WG source appearance, so the command needs to know which ones."
        )
    positions = _indices(entries, "face")
    paint: list[int] = []
    clear: list[int] = []
    unchanged: list[int] = []
    for position, entry in enumerate(entries):
        index = positions[position]
        current = _canonical_role(entry.get("current_role"))
        if role is None:
            (clear if current is not None else unchanged).append(index)
        elif current == role:
            unchanged.append(index)
        else:
            paint.append(index)

    if role is None:
        summary = f"Cleared the WG source role from {_faces_phrase(len(clear))}."
        if not clear:
            summary = (
                "No selected face carried a WG source role, so nothing changed."
            )
        elif unchanged:
            summary += (
                f" {_faces_phrase(len(unchanged))} carried no WG role and kept "
                f"{'its' if len(unchanged) == 1 else 'their'} own appearance."
            )
    else:
        carrying = set(paint) | set(unchanged)
        area = _total_area_mm2(entries, carrying, positions)
        measured = f" ({area:.1f} mm² in total)" if area is not None else ""
        summary = (
            f"{_faces_phrase(len(carrying))} now drive the {role} source"
            f"{measured}."
        )
        if paint and unchanged:
            summary += f" {_faces_phrase(len(paint))} {_was(len(paint))} repainted."
        elif not paint:
            summary = (
                f"Every selected face already carried {role}"
                f"{measured}; nothing changed."
            )
    return SourcePlan(
        role=role,
        appearance_name=role_appearance_name(role) if role is not None else None,
        paint=tuple(paint),
        clear=tuple(clear),
        unchanged=tuple(unchanged),
        summary=summary,
    )


# ------------------------------------------------------------- body declaration

BODY_DECLARATIONS = ("exterior-shell", "exclude")
CLEAR_DECLARATION_LABEL = "Clear declaration"
DECLARATION_CHOICES = (
    ("Exterior shell — solve this body's outer surface", "exterior-shell"),
    ("Exclude — leave this body out of the return", "exclude"),
    (CLEAR_DECLARATION_LABEL, None),
)

_DECLARATION_HELP = {
    "exterior-shell": (
        "Declares the selected surface body as the exterior WG solves. A visible "
        "surface body with no declaration refuses the export, because WG cannot "
        "tell a horn shell from modelling scaffolding."
    ),
    "exclude": (
        "Leaves the selected bodies out of the WG return entirely: sketch "
        "scaffolding, fixtures, and anything that is not part of the acoustic "
        "exterior."
    ),
    None: (
        "Removes the declaration and puts the selected bodies back under WG's "
        "automatic classification."
    ),
}


# ------------------------------------------------------------ model domain

# A domain declaration is a property of the *model being returned*, not of one
# body, which is why it lives on the Send/Solve dialog rather than in Declare
# Body. It says the author already cut the model and the solver must supply the
# missing half by mirroring -- so it is only ever chosen by a person who knows
# that, and the default is the one that is never wrong.
DOMAIN_PLANES = ("x0", "y0")
AXIS_FOR_DOMAIN_PLANE = {"x0": 0, "y0": 1}
DOMAIN_PLANE_LABEL = {"x0": "x = 0", "y0": "y = 0"}
FULL_DOMAIN_LABEL = "Full model"
DOMAIN_CHOICES = (
    (FULL_DOMAIN_LABEL, ()),
    ("Half — already cut on x = 0", ("x0",)),
    ("Half — already cut on y = 0", ("y0",)),
    ("Quarter — already cut on x = 0 and y = 0", ("x0", "y0")),
)

_DOMAIN_HELP = {
    (): (
        "The exported bodies are the whole model. WG looks for mirror symmetry "
        "itself and cuts the model down when it finds it."
    ),
    ("x0",): (
        "The exported bodies are already half a model, kept on x ≥ 0. WG mirrors "
        "them about x = 0 instead of cutting, and the open face on that plane is "
        "the mirror — not a hole and not a baffle."
    ),
    ("y0",): (
        "The exported bodies are already half a model, kept on y ≥ 0. WG mirrors "
        "them about y = 0 instead of cutting, and the open face on that plane is "
        "the mirror — not a hole and not a baffle."
    ),
    ("x0", "y0"): (
        "The exported bodies are already a quarter model, kept on x ≥ 0 and "
        "y ≥ 0. WG mirrors them about both planes instead of cutting."
    ),
}


def domain_choices() -> tuple[str, ...]:
    """The dropdown items of the model-domain selector, in display order."""

    return tuple(label for label, _planes in DOMAIN_CHOICES)


def resolve_domain_choice(choice: object) -> tuple[str, ...]:
    """Turn a dropdown item -- or a plane list -- into canonical planes."""

    if isinstance(choice, (list, tuple)):
        planes = tuple(str(plane).strip().lower() for plane in choice)
        unknown = [plane for plane in planes if plane not in DOMAIN_PLANES]
        if unknown:
            raise AuthorError(
                f"{', '.join(repr(plane) for plane in unknown)} is not a symmetry "
                f"plane. Choose from {', '.join(DOMAIN_PLANES)}."
            )
        return tuple(plane for plane in DOMAIN_PLANES if plane in set(planes))
    value = str(choice if choice is not None else "").strip()
    if not value:
        raise AuthorError(f"Choose a model domain, or {FULL_DOMAIN_LABEL!r}.")
    for label, planes in DOMAIN_CHOICES:
        if value.casefold() == label.casefold():
            return planes
    folded = value.casefold()
    if folded in {"full", FULL_DOMAIN_LABEL.casefold()}:
        return ()
    if folded in DOMAIN_PLANES:
        return (folded,)
    raise AuthorError(
        f"{value!r} is not a model domain. Choose one of "
        f"{', '.join(domain_choices())}."
    )


def domain_help_text(choice: object) -> str:
    return _DOMAIN_HELP[resolve_domain_choice(choice)]


def domain_phrase(planes: Sequence[str]) -> str:
    """How a declared domain reads in one line of dialog copy."""

    canonical = resolve_domain_choice(planes)
    if not canonical:
        return "full model"
    labels = " and ".join(DOMAIN_PLANE_LABEL[plane] for plane in canonical)
    noun = "half" if len(canonical) == 1 else "quarter"
    return f"{noun} model, already cut on {labels}"


def declaration_choices() -> tuple[str, ...]:
    """The dropdown items of the Declare Body command, in display order."""

    return tuple(label for label, _value in DECLARATION_CHOICES)


def resolve_declaration_choice(choice: object) -> str | None:
    """Turn a dropdown item -- or a raw declaration -- into a stored value."""

    value = str(choice if choice is not None else "").strip()
    if not value:
        raise AuthorError(
            f"Choose a body declaration, or {CLEAR_DECLARATION_LABEL!r}."
        )
    for label, declaration in DECLARATION_CHOICES:
        if value.casefold() == label.casefold():
            return declaration
    folded = value.casefold()
    if folded in {CLEAR_DECLARATION_LABEL.casefold(), "clear", "none"}:
        return None
    if folded in BODY_DECLARATIONS:
        return folded
    raise AuthorError(
        f"{value!r} is not a body declaration. Choose one of "
        f"{', '.join(BODY_DECLARATIONS)}, or {CLEAR_DECLARATION_LABEL!r}."
    )


def declaration_help_text(choice: object) -> str:
    return _DECLARATION_HELP[resolve_declaration_choice(choice)]


@dataclass(frozen=True)
class DeclarationPlan:
    """Which of the selected bodies get an attribute written or removed."""

    declaration: str | None
    write: tuple[int, ...]
    clear: tuple[int, ...]
    unchanged: tuple[int, ...]
    summary: str


def _bodies_phrase(count: int) -> str:
    return "1 body" if count == 1 else f"{count} bodies"


def plan_body_declaration(
    bodies: Sequence[Mapping[str, Any]], choice: object
) -> DeclarationPlan:
    """Decide which selected bodies need their declaration attribute touched."""

    declaration = resolve_declaration_choice(choice)
    entries = list(bodies)
    if not entries:
        raise AuthorError("Select at least one body to declare.")
    positions = _indices(entries, "body")
    write: list[int] = []
    clear: list[int] = []
    unchanged: list[int] = []
    for position, entry in enumerate(entries):
        index = positions[position]
        current = entry.get("current")
        current = str(current) if current in BODY_DECLARATIONS else None
        if current == declaration:
            unchanged.append(index)
        elif declaration is None:
            clear.append(index)
        else:
            write.append(index)

    if declaration is None:
        summary = (
            f"Cleared the WG declaration on {_bodies_phrase(len(clear))}."
            if clear
            else "No selected body carried a WG declaration, so nothing changed."
        )
    else:
        changed = len(write)
        summary = (
            f"Declared {_bodies_phrase(changed)} as {declaration}."
            if changed
            else f"Every selected body was already {declaration}; nothing changed."
        )
        if declaration == "exterior-shell" and write:
            solids = [
                entry
                for position, entry in enumerate(entries)
                if positions[position] in set(write)
                and str(entry.get("body_kind") or "") == "solid"
            ]
            if len(solids) == len(write):
                summary += (
                    " Solid bodies are already included automatically; the "
                    "declaration matters for surface bodies."
                )
    if unchanged and (write or clear):
        summary += (
            f" {_bodies_phrase(len(unchanged))} {_was(len(unchanged))} already correct."
        )
    return DeclarationPlan(
        declaration=declaration,
        write=tuple(write),
        clear=tuple(clear),
        unchanged=tuple(unchanged),
        summary=summary,
    )


# ------------------------------------------------------------- solver frame

FRAME_AXIS = "axis"
FRAME_THROAT_Z = "throat-z"
FRAME_CENTRING = "centring"

# An unlinked return carries no throat frame, so the solver adopts the assembly
# frame unchanged. These tolerances only decide when to *say something*; the
# solver itself never checks and never refuses.
THROAT_Z_TOLERANCE_MM = 1.0
CENTRING_TOLERANCE_MM = 1.0
CENTRING_RELATIVE_TOLERANCE = 0.02


@dataclass(frozen=True)
class FrameFinding:
    """One way the model departs from WG's unlinked solver frame."""

    code: str
    message: str


def _vector(value: object) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    numbers = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        numbers.append(number)
    return (numbers[0], numbers[1], numbers[2])


def _bounds(value: object) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    if not isinstance(value, Mapping):
        return None
    low = _vector(value.get("min"))
    high = _vector(value.get("max"))
    if low is None or high is None:
        return None
    if any(high[axis] < low[axis] for axis in range(3)):
        return None
    return low, high


def frame_findings(
    bounds_mm: object,
    source_bounds_mm: object = None,
    domain_planes: Sequence[str] = (),
) -> list[FrameFinding]:
    """Compare a model's extent against WG's unlinked solver frame.

    Advisory only, and only meaningful for an unlinked return: a linked return
    carries its own throat frame and is transformed into the solver frame on
    ingestion.

    ``domain_planes`` names the planes the export declares it was already cut
    on. A half is *correctly* off-centre about its own cut plane -- it occupies
    the positive side and nothing else -- so the centring advice would be
    exactly wrong there, and telling a user to re-centre a half is telling them
    to break it.
    """

    reduced_axes = {AXIS_FOR_DOMAIN_PLANE[str(plane)] for plane in domain_planes}

    bounds = _bounds(bounds_mm)
    if bounds is None:
        return []
    low, high = bounds
    findings: list[FrameFinding] = []
    source = _bounds(source_bounds_mm)
    throat_z = None if source is None else 0.5 * (source[0][2] + source[1][2])

    if throat_z is not None:
        if (high[2] - throat_z) < (throat_z - low[2]):
            findings.append(FrameFinding(
                FRAME_AXIS,
                "the model extends toward -Z from its source face, but WG "
                "radiates an unlinked model along +Z. Rotate the model so the "
                "mouth faces +Z.",
            ))
        if abs(throat_z) > THROAT_Z_TOLERANCE_MM:
            findings.append(FrameFinding(
                FRAME_THROAT_Z,
                f"the source face sits at z = {throat_z:.1f} mm, but WG puts an "
                "unlinked model's throat at z = 0. Move the model along Z.",
            ))
    elif high[2] <= 0.0 and low[2] < 0.0:
        findings.append(FrameFinding(
            FRAME_AXIS,
            "the model lies entirely at negative z, but WG radiates an unlinked "
            "model along +Z. Rotate the model so the mouth faces +Z.",
        ))

    offsets = []
    for axis, name in ((0, "x"), (1, "y")):
        if axis in reduced_axes:
            continue
        centre = 0.5 * (low[axis] + high[axis])
        extent = high[axis] - low[axis]
        tolerance = max(
            CENTRING_TOLERANCE_MM, CENTRING_RELATIVE_TOLERANCE * abs(extent)
        )
        if abs(centre) > tolerance:
            offsets.append(f"{name} = {centre:.1f} mm")
    if offsets:
        findings.append(FrameFinding(
            FRAME_CENTRING,
            f"the bounding box is centred on {' and '.join(offsets)}, but WG "
            "expects an unlinked model centred on x = 0 and y = 0.",
        ))
    return findings


# ----------------------------------------------------------------- pre-flight

NO_SOURCE_WARNING = (
    "No drivable source: use Set WG Source… to mark a face LF, MF, HF, or "
    "PASSIVE_CARDIOID. The export refuses without one."
)
DECLARE_BODY_HINT = "Use Declare Body… to classify it or leave it out."
LOG_HINT = (
    "The full traceback is in Fusion's Text Commands palette "
    "(View → Show Text Commands)."
)

_BODY_KIND_NOUNS = (
    ("solid", "solid", "solids"),
    ("surface", "surface body", "surface bodies"),
    ("mesh", "mesh body", "mesh bodies"),
)


@dataclass(frozen=True)
class Preflight:
    """What the Send and Solve dialogs state before anything is exported."""

    lines: tuple[str, ...]
    warnings: tuple[str, ...]
    frame: tuple[FrameFinding, ...]

    def text(self) -> str:
        blocks = list(self.lines)
        if self.warnings:
            blocks.append("")
            blocks.extend(f"⚠ {warning}" for warning in self.warnings)
        return "\n".join(blocks)

    def html(self) -> str:
        rows = [html.escape(line) for line in self.lines]
        if self.warnings:
            rows.append("")
            rows.extend(
                f"<b>⚠ {html.escape(warning)}</b>" for warning in self.warnings
            )
        return "<br>".join(rows)


def _body_count_phrase(included: list[Mapping[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for item in included:
        kind = str(item.get("body_kind") or "solid")
        counts[kind] = counts.get(kind, 0) + 1
    phrases = []
    for kind, singular, plural in _BODY_KIND_NOUNS:
        count = counts.pop(kind, 0)
        if count:
            phrases.append(f"{count} {singular if count == 1 else plural}")
    for kind in sorted(counts):
        phrases.append(f"{counts[kind]} {kind}")
    return ", ".join(phrases) if phrases else "none"


def preflight_summary(scope: Mapping[str, Any]) -> Preflight:
    """Compose the read-only Send/Solve summary from plain gathered numbers.

    ``scope`` is what ``wglink_send.preflight_scope`` gathers on Fusion's main
    thread: counts, ids, source roles and areas, and two bounding boxes. No
    Fusion object ever reaches this function.
    """

    if not isinstance(scope, Mapping):
        raise AuthorError("Pre-flight needs the gathered scope report.")
    selection = str(scope.get("selection") or "root")
    included = [
        item for item in (scope.get("included") or []) if isinstance(item, Mapping)
    ]
    instance_ids = [str(value) for value in (scope.get("instance_ids") or [])]
    sources = [
        item for item in (scope.get("sources") or []) if isinstance(item, Mapping)
    ]
    scope_error = str(scope.get("scope_error") or "").strip()
    source_error = str(scope.get("source_error") or "").strip()
    domain_error = str(scope.get("domain_error") or "").strip()
    domain = scope.get("domain")
    domain_planes = (
        tuple(str(plane) for plane in (domain.get("cut_planes") or []))
        if isinstance(domain, Mapping)
        else ()
    )

    lines = [
        "Scope: root assembly" if selection == "root" else f"Scope: {selection}",
        f"Bodies included: {_body_count_phrase(included)}",
    ]
    if instance_ids:
        lines.append(
            f"WG links in scope: {len(instance_ids)} ({', '.join(instance_ids)})"
        )
    else:
        lines.append(
            "WG links in scope: none — this exports as an unlinked "
            "(Fusion-first) return."
        )
    if sources:
        for source in sources:
            role = str(source.get("role") or "?")
            faces = source.get("face_count")
            faces = int(faces) if isinstance(faces, int) else 0
            area = source.get("area_mm2")
            measured = (
                f"{float(area):.1f} mm²"
                if isinstance(area, (int, float)) and not isinstance(area, bool)
                else "area unread"
            )
            instance_id = str(source.get("instance_id") or "").strip()
            suffix = f", from link {instance_id}" if instance_id else ""
            lines.append(
                f"Source: {role} — {measured} over {_faces_phrase(faces)}{suffix}"
            )
    else:
        lines.append("Sources: none")
    if domain_planes:
        lines.append(f"Domain: {domain_phrase(domain_planes)} ✓")

    warnings: list[str] = []
    if domain_error:
        warnings.append(f"Model domain: {domain_error}")
    if scope_error:
        warning = f"Export is blocked: {scope_error}"
        if "unclassified" in scope_error.casefold():
            warning += f" {DECLARE_BODY_HINT}"
        warnings.append(warning)
    if not sources:
        warnings.append(source_error or NO_SOURCE_WARNING)

    findings: list[FrameFinding] = []
    if not instance_ids:
        findings = frame_findings(
            scope.get("bounds_mm"),
            scope.get("source_bounds_mm"),
            domain_planes,
        )
        if findings:
            warnings.extend(f"Solver frame: {item.message}" for item in findings)
        elif _bounds(scope.get("bounds_mm")) is not None:
            centred = " and ".join(
                f"{name} = 0"
                for axis, name in ((0, "x"), (1, "y"))
                if axis not in {AXIS_FOR_DOMAIN_PLANE[plane] for plane in domain_planes}
            )
            lines.append(
                "Solver frame: axis +Z, throat at z = 0"
                + (f", centred on {centred}" if centred else "")
                + " ✓"
            )
    return Preflight(
        lines=tuple(lines), warnings=tuple(warnings), frame=tuple(findings)
    )


# ------------------------------------------------------- user-facing failures


def _detail(exc: BaseException | None) -> str:
    if exc is None:
        return "no details were reported"
    text = " ".join(str(exc).split())
    if not text:
        text = exc.__class__.__name__
    return text if len(text) <= 200 else text[:197] + "…"


def failure_message(action: str, exc: BaseException | None) -> str:
    """One human line for a message box; the traceback goes to the log.

    A raw ``traceback.format_exc()`` in a modal states the add-in's internals
    and nothing the user can act on, and it hides the one sentence that would
    have identified the failure.
    """

    return f"{action} hit an unexpected error: {_detail(exc)}\n\n{LOG_HINT}"


def preflight_unavailable(exc: BaseException | None) -> str:
    """Text for the pre-flight box when the model could not be surveyed."""

    return f"Pre-flight unavailable: {_detail(exc)}"
