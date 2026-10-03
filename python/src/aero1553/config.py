"""Configuration loading and management for Aero1553.

Loads configuration from TOML files and merges with CLI arguments.
CLI arguments always take precedence over file-based configuration.

The TOML grammar, the schema checks and the unknown-key WARN are
``rust/src/config.rs``, run through the package's compiled extension, so a
config file means the same thing to the library as to the CLI.
:class:`DecoderConfig` and :class:`FilterConfig` are Python dataclasses built
from what that loader returns.

Configuration sources (in priority order, highest first):
    1. CLI arguments (``--log-level``, ``--input-time-format``, ``--exclude-types``, etc.)
    2. User-specified config file (``--config path/to/config.toml``)
    3. Built-in defaults (equivalent to ``config/default.toml``)

Usage::

    from aero1553.config import DecoderConfig, load_config

    # Load from file
    config = load_config("my-config.toml")

    # Override with CLI args
    config = config.with_overrides(log_level="DEBUG", exclude_types=["SPURIOUS_DATA"])
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aero1553 import _native
from aero1553.merge import DEFAULT_MAX_COLLAPSE_SURVIVORS
from aero1553.models import (
    Bus,
    DeltaScope,
    ErrorMode,
    OutputTimeFormat,
    TimestampFormat,
)
from aero1553.order import DEFAULT_MAX_SORT_GROUP

logger = logging.getLogger(__name__)

#: L2-DEC-015 valid range for ``decode.detect_records``. Values outside
#: this range are rejected at config-load time with a clear error.
DETECT_RECORDS_MIN: int = 1
DETECT_RECORDS_MAX: int = 32

#: L2-SYN-026 valid range for ``decode.lookahead_records``. Same shape
#: as DETECT_RECORDS_MIN/_MAX — both configurable record-count knobs
#: share their valid range for consistency.
LOOKAHEAD_RECORDS_MIN: int = 1
LOOKAHEAD_RECORDS_MAX: int = 32


@dataclass
class FilterConfig:
    """Message filtering configuration.

    Both ``exclude_*`` (negative) and ``include_*`` (positive) filters
    are supported, mirroring the Rust ``FilterConfig``. A message passes
    if it matches no active ``exclude_*`` set AND every active
    ``include_*`` set contains its value. Empty (inactive) sets are
    ignored on both sides; excludes are checked first and take
    precedence.

    Attributes:
        exclude_types: MessageType values to exclude. Empty = no filter.
        exclude_rts: RT addresses (0–31) to exclude. Empty = no filter.
        exclude_buses: Bus values to exclude. Empty = no filter.
        exclude_subaddresses: subaddresses (0–31) to exclude. Empty = no
            filter.
        include_types: when non-empty, only these MessageType values pass.
        include_rts: when non-empty, only these RT addresses pass.
        include_buses: when non-empty, only these Bus values pass.
        include_subaddresses: when non-empty, only these subaddresses pass.
    """

    exclude_types: set[int] = field(default_factory=set)
    exclude_rts: set[int] = field(default_factory=set)
    exclude_buses: set[Bus] = field(default_factory=set)
    exclude_subaddresses: set[int] = field(default_factory=set)

    include_types: set[int] = field(default_factory=set)
    include_rts: set[int] = field(default_factory=set)
    include_buses: set[Bus] = field(default_factory=set)
    include_subaddresses: set[int] = field(default_factory=set)

    @property
    def is_active(self) -> bool:
        """True if any filter criteria are configured."""
        return bool(
            self.exclude_types
            or self.exclude_rts
            or self.exclude_buses
            or self.exclude_subaddresses
            or self.include_types
            or self.include_rts
            or self.include_buses
            or self.include_subaddresses
        )

    def should_exclude(
        self, message_type: int, rt: int | None, bus: Bus, subaddress: int | None
    ) -> bool:
        """Test whether a message should be excluded from output.

        Args:
            message_type: The message type code from the Type Word.
            rt: The Remote Terminal address from the Command Word, or
                ``None`` for records with no Command Word (SPURIOUS_DATA).
            bus: The bus identifier from the Type Word.
            subaddress: The subaddress from the Command Word, or ``None``
                for records with no Command Word (SPURIOUS_DATA).

        Returns:
            True if the message matches any ``exclude_*`` criterion, or
            fails any active ``include_*`` criterion. A ``None``
            ``rt``/``subaddress`` never matches an RT/subaddress exclude
            filter, and is always dropped when an RT/subaddress include
            filter is active (SPURIOUS_DATA has no RT/SA). Mirrors the
            Rust ``FilterConfig::should_exclude`` behavior.

            This judges one record on its own fields. In a stream,
            :func:`~aero1553.filters.apply_filters` decides a ``0x2000``
            continuation through its errored parent instead (L2-FLT-003).
        """
        # Negative filters take precedence over positive ones.
        return self._matches_exclude(message_type, rt, bus, subaddress) or self._fails_include(
            message_type, rt, bus, subaddress
        )

    def _matches_exclude(
        self, message_type: int, rt: int | None, bus: Bus, subaddress: int | None
    ) -> bool:
        """Whether the message matches any active ``exclude_*`` set.

        Returns:
            ``True`` if any active exclude set matches, meaning the message is
            dropped. An empty set is inactive and never matches.
        """
        return bool(
            (self.exclude_types and message_type in self.exclude_types)
            or (self.exclude_rts and rt in self.exclude_rts)
            or (self.exclude_buses and bus in self.exclude_buses)
            or (self.exclude_subaddresses and subaddress in self.exclude_subaddresses)
        )

    def _fails_include(
        self, message_type: int, rt: int | None, bus: Bus, subaddress: int | None
    ) -> bool:
        """Whether the message is absent from any active ``include_*`` set. A
        ``None`` rt/subaddress (SPURIOUS_DATA) fails an active RT/SA include.

        Returns:
            ``True`` if the message is absent from an active include set,
            meaning it is dropped. An empty set is inactive and admits
            everything.
        """
        return bool(
            (self.include_types and message_type not in self.include_types)
            or (self.include_buses and bus not in self.include_buses)
            or (self.include_rts and (rt is None or rt not in self.include_rts))
            or (
                self.include_subaddresses
                and (subaddress is None or subaddress not in self.include_subaddresses)
            )
        )


@dataclass
class DecoderConfig:
    """Complete decoder configuration.

    Attributes:
        log_level: Logging verbosity level name.
        irig_day_advisory: L2-LOG-001. Emit the one-time IRIG day-of-year
            advisory. True by default, but the advisory is logged at INFO,
            so at the default WARNING level it is already silent -- this
            exists so a site that has validated its card model against
            vendor CSV can also keep it out of a ``--log-level INFO`` run.
        input_time_format: Timestamp format (auto/irig/standard).
        strict: If True, raise on invalid records instead of skipping.
        error_mode: How errored messages appear in output.
        filters: Message filtering configuration.
        output_format: Output format name (currently only ``csv``).
        no_clobber: L2-WRT-017. Refuse to overwrite an existing
            destination. Defaults to False (overwrite permitted).
    """

    log_level: str = "WARNING"
    irig_day_advisory: bool = True
    input_time_format: TimestampFormat = TimestampFormat.AUTO
    strict: bool = False
    error_mode: ErrorMode = ErrorMode.INLINE
    filters: FilterConfig = field(default_factory=FilterConfig)
    output_format: str = "csv"
    no_clobber: bool = False
    #: L2-WRT-025: which rendering the TIME_STAMP column uses. Defaults to
    #: ``doy`` -- the DDC vendor rendering -- so a decode that selects nothing
    #: stays byte-comparable against vendor CSV.
    output_time_format: OutputTimeFormat = OutputTimeFormat.DOY
    #: L2-WRT-026: calendar year used to resolve the IRIG day-of-year field.
    #: Required by ``iso`` / ``dom``, inert under ``doy``. An MIE file carries
    #: no year, so None -- "not supplied" -- is the only honest default, and a
    #: calendar rendering with None here is refused rather than guessed at.
    year: int | None = None
    #: L2-WRT-025: offset from UTC in minutes for the ``iso`` zone
    #: designator. Zero renders as ``Z``.
    utc_offset_minutes: int = 0
    allow_partial: bool = False
    #: L2-DEC-015: number of records the timestamp-format auto-detect
    #: probe walks before committing to IRIG vs Standard. Range
    #: [DETECT_RECORDS_MIN, DETECT_RECORDS_MAX]. Default 8.
    detect_records: int = _native.DEFAULT_DETECT_RECORDS
    #: L2-SYN-026: total number of records sync validation checks
    #: (1 candidate + N-1 look-ahead). Range
    #: [LOOKAHEAD_RECORDS_MIN, LOOKAHEAD_RECORDS_MAX].
    #:
    #: Both defaults are the core crate's own constants (``decode.rs`` /
    #: ``sync.rs``), read from the extension rather than repeated here: a
    #: repeated value is how this default once drifted from Rust's.
    lookahead_records: int = _native.DEFAULT_LOOKAHEAD_RECORDS
    #: L2-DEC-017: optional Standard-counter tick rate in Hz. None (the
    #: default) keeps the historical empty-DELTA behavior for Standard
    #: records; a finite, strictly-positive value enables tick->microsecond
    #: conversion and DELTA participation. Validated at load time.
    standard_tick_rate_hz: float | None = None
    #: L2-WRT-020: populate the MUX column from a field of the input file
    #: name. Enabled by default ([mux] enabled = false / --no-mux disables
    #: it for vendor-exact output). The name is split on mux_delimiter and the
    #: mux_field-th field (0-based; negative counts from the end) becomes MUX.
    mux_enabled: bool = True
    mux_delimiter: str = "."
    mux_field: int = 4

    #: L2-MRG-007: collapse the same bus transaction witnessed by multiple
    #: recorders into one row, in a multi-file merge. Off by default (loss-free).
    #: collapse_window_us is the timestamp tolerance in microseconds (0 = exact).
    collapse_duplicates: bool = False
    collapse_window_us: int = 0
    #: L2-MRG-008: cap on the survivors the de-duplication window (L2-MRG-007)
    #: retains at once. Validated at load time against
    #: [MAX_COLLAPSE_SURVIVORS_MIN, MAX_COLLAPSE_SURVIVORS_MAX]. Default 4096.
    #: The window bounds retention in time; this bounds it in count, so input
    #: whose timestamps all decode alike cannot grow the set without limit.
    max_collapse_survivors: int = DEFAULT_MAX_COLLAPSE_SURVIVORS

    #: L2-MRG-005: scope over which DELTA is measured in a multi-file merge.
    #: PER_FILE (default) leaves each reader's own DELTA in place, matching a
    #: single-file decode and the vendor tool; GLOBAL measures across the merged
    #: timeline. No effect on a single-input decode.
    delta_scope: DeltaScope = DeltaScope.PER_FILE

    #: L2-WRT-022: cap on the number of consecutive equal-TIME_STAMP records the
    #: canonical-order stage (L2-WRT-021) buffers at once. Range
    #: [MAX_SORT_GROUP_MIN, MAX_SORT_GROUP_MAX]. Default 65536; 1 disables
    #: reordering, restoring raw DDC capture order.
    max_sort_group: int = DEFAULT_MAX_SORT_GROUP

    def with_overrides(self, **kwargs: Any) -> DecoderConfig:
        """Return a new config with specified fields overridden.

        Only non-None values in kwargs are applied.

        Args:
            **kwargs: Field names and values to override.

        Returns:
            A new DecoderConfig with the overrides applied.
        """
        return DecoderConfig(
            log_level=self._override_present(kwargs, "log_level"),
            irig_day_advisory=self._override_present(kwargs, "irig_day_advisory"),
            input_time_format=self._override_present(kwargs, "input_time_format"),
            strict=self._override_present(kwargs, "strict"),
            error_mode=self._override_present(kwargs, "error_mode"),
            filters=self._merge_filter_overrides(kwargs),
            output_format=self._override_present(kwargs, "output_format"),
            no_clobber=self._override_present(kwargs, "no_clobber"),
            output_time_format=self._override_present(kwargs, "output_time_format"),
            year=self._override_present(kwargs, "year"),
            utc_offset_minutes=self._override_present(kwargs, "utc_offset_minutes"),
            allow_partial=self._override_present(kwargs, "allow_partial"),
            detect_records=self._override_present(kwargs, "detect_records"),
            lookahead_records=self._override_present(kwargs, "lookahead_records"),
            standard_tick_rate_hz=self._override_present(kwargs, "standard_tick_rate_hz"),
            mux_enabled=self._override_present(kwargs, "mux_enabled"),
            mux_delimiter=self._override_present(kwargs, "mux_delimiter"),
            mux_field=self._override_present(kwargs, "mux_field"),
            collapse_duplicates=self._override_present(kwargs, "collapse_duplicates"),
            collapse_window_us=self._override_present(kwargs, "collapse_window_us"),
            max_collapse_survivors=self._override_present(kwargs, "max_collapse_survivors"),
            max_sort_group=self._override_present(kwargs, "max_sort_group"),
            delta_scope=self._override_present(kwargs, "delta_scope"),
        )

    def _override_present(self, kwargs: dict[str, Any], name: str) -> Any:
        """Override resolution for every scalar field: only an explicit
        non-``None`` override replaces the current value, so an omitted CLI flag
        never resets a config-file value (e.g. ``no_clobber = true``).

        Presence — not truthiness — is the test, mirroring Rust's
        ``Option<T>``-based ``DecoderConfig::with_overrides`` (``rust/src/config.rs``)
        exactly. An earlier truthiness-based variant silently **dropped** any
        override whose value was falsy, which made ``--input-time-format auto``
        (``TimestampFormat.AUTO == 0``) a no-op against a config file that set
        ``input_time_format = "irig"`` — the config value won, Rust used ``auto``, and
        the two implementations decoded the same file differently. The same trap
        applied to ``ErrorMode.SEPARATE == 0`` and to any empty-string value.

        Returns:
            The override when it is not ``None``, otherwise the current value
            of the field. Presence, not truthiness, decides.
        """
        value = kwargs.get(name)
        return value if value is not None else getattr(self, name)

    def _merge_filter_overrides(self, kwargs: dict[str, Any]) -> FilterConfig:
        """CLI filters ADD to (not replace) config-file filters (Rust parity);
        ``include_*`` are CLI-only but merge the same way for symmetry.

        Returns:
            A new :class:`FilterConfig` whose sets are the union of the config
            file's and the CLI's. CLI filters add to, never replace.
        """
        return FilterConfig(
            exclude_types=self.filters.exclude_types | set(kwargs.get("exclude_types") or []),
            exclude_rts=self.filters.exclude_rts | set(kwargs.get("exclude_rts") or []),
            exclude_buses=self.filters.exclude_buses | set(kwargs.get("exclude_buses") or []),
            exclude_subaddresses=(
                self.filters.exclude_subaddresses | set(kwargs.get("exclude_subaddresses") or [])
            ),
            include_types=self.filters.include_types | set(kwargs.get("include_types") or []),
            include_rts=self.filters.include_rts | set(kwargs.get("include_rts") or []),
            include_buses=self.filters.include_buses | set(kwargs.get("include_buses") or []),
            include_subaddresses=(
                self.filters.include_subaddresses | set(kwargs.get("include_subaddresses") or [])
            ),
        )


def load_config(path: str | Path | None = None) -> DecoderConfig:
    """Load configuration from a TOML file.

    Args:
        path: Path to the TOML configuration file. If None, returns
            the built-in defaults.

    Returns:
        A populated DecoderConfig.

    Raises:
        FileNotFoundError: If the specified config file does not exist.
        ValueError: If the path is not a regular file, or the file holds
            TOML the loader rejects or a value outside the schema.
    """
    if path is None:
        logger.debug("No config file specified, using defaults")
        return DecoderConfig()
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    logger.info("Loading config from %s", config_path)
    fields = _native.load_config(config_path)
    f = fields.pop("filters")
    # The loader returns each enum as its integer value.
    fields["input_time_format"] = TimestampFormat(fields["input_time_format"])
    fields["error_mode"] = ErrorMode(fields["error_mode"])
    fields["output_time_format"] = OutputTimeFormat(fields["output_time_format"])
    fields["delta_scope"] = DeltaScope(fields["delta_scope"])
    config = DecoderConfig(
        **fields,
        filters=FilterConfig(
            exclude_types=set(f["exclude_types"]),
            exclude_rts=set(f["exclude_rts"]),
            exclude_buses={Bus(b) for b in f["exclude_buses"]},
            exclude_subaddresses=set(f["exclude_subaddresses"]),
            include_types=set(f["include_types"]),
            include_rts=set(f["include_rts"]),
            include_buses={Bus(b) for b in f["include_buses"]},
            include_subaddresses=set(f["include_subaddresses"]),
        ),
    )
    logger.debug("Loaded config: %s", config)
    return config


def parse_utc_offset(text: str) -> int:
    """Parse a UTC offset designator into minutes east of UTC (L2-CFG-012).

    Accepts ``Z`` (case-insensitively) for zero, or a signed ``+HH:MM`` /
    ``-HH:MM``. The shape is checked exactly rather than leniently: ``+5:00``,
    ``+0500`` and a trailing-space variant are all rejected, because a zone that
    parsed loosely here would silently shift every ISO timestamp in the file.

    Returns:
        The offset in minutes; negative west of UTC.

    Raises:
        ValueError: if the text does not match the grammar or is out of range.
    """
    return _native.parse_utc_offset(text)
