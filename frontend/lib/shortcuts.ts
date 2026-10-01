/**
 * shortcuts.ts — the Signal-OS keyboard shortcut system.
 *
 * Three concerns, kept separate:
 *
 *   1. A typed **command registry** — every user-invokable action, its
 *      keybinding and category ({@link SHORTCUT_COMMANDS}).
 *   2. A pure **binding parser + matcher** ({@link parseBinding},
 *      {@link matchesEvent}) with cross-platform normalisation: the same
 *      logical binding reads ⌘K on macOS and Ctrl+K elsewhere.
 *   3. A **dispatcher hook** ({@link useHotkeys}) that wires the above to a
 *      window `keydown` listener, with input-focus and propagation guards.
 *
 * The matcher is deliberately free of React so it can be unit-tested and reused
 * by future non-DOM consumers.
 */

"use client";

import { useEffect, useRef } from "react";

// ── Binding model ───────────────────────────────────────────────────────────

/** Modifier keys a binding may require. */
export interface ModifierState {
  readonly metaKey?: boolean;
  readonly ctrlKey?: boolean;
  readonly shiftKey?: boolean;
  readonly altKey?: boolean;
}

/** A logical keybinding: `mod` + shift/alt + a normalized `key`. */
export interface KeyBinding extends ModifierState {
  /**
   * Normalized non-modifier key. Single characters are lower-cased (`"k"`,
   * `"/"`); named keys use their DOM `KeyboardEvent.key` value
   * (`"enter"`, `"escape"`, `"arrowdown"`).
   */
  readonly key: string;
}

/**
 * A binding as authored by us, before platform resolution. `mod` means "the
 * platform's primary modifier" — ⌘ on Apple, Ctrl everywhere else.
 */
export interface BindingSpec {
  readonly key: string;
  /** Primary modifier: ⌘ on macOS, Ctrl elsewhere. */
  readonly mod?: boolean;
  readonly ctrl?: boolean;
  readonly meta?: boolean;
  readonly shift?: boolean;
  readonly alt?: boolean;
  /** When true this binding stays active even if an input has focus. */
  readonly allowInInput?: boolean;
}

/** Enough of `KeyboardEvent` to match against, without depending on the DOM. */
export interface KeyEventLike {
  readonly key: string;
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly shiftKey: boolean;
  readonly altKey: boolean;
  /** Present on real events; absent on synthetic ones. */
  readonly repeat?: boolean;
  readonly target?: EventTarget | null;
}

/** Groups a command in the shortcuts dialog. */
export type ShortcutCategory =
  | "Navigation"
  | "Investigation"
  | "View"
  | "Editing"
  | "System";

/** All categories in display order. */
export const SHORTCUT_CATEGORIES: readonly ShortcutCategory[] = [
  "Navigation",
  "Investigation",
  "View",
  "Editing",
  "System",
] as const;

/** Stable id of a command — what the dispatcher actually acts on. */
export type CommandId =
  | "palette.open"
  | "palette.toggle"
  | "investigation.run"
  | "investigation.new"
  | "investigation.focus"
  | "view.graph"
  | "view.timeline"
  | "view.reports"
  | "view.explain"
  | "view.toggleEntities"
  | "edit.export"
  | "edit.copy"
  | "theme.toggle"
  | "shortcuts.open"
  | "onboarding.restart"
  | "dialog.close";

/** One row of the shortcuts dialog / command palette. */
export interface ShortcutCommand {
  readonly id: CommandId;
  /** Human label. */
  readonly label: string;
  /** Longer explanation shown in the dialog. */
  readonly description: string;
  /** Authored binding; resolved per-platform at render time. */
  readonly binding: BindingSpec;
  readonly category: ShortcutCategory;
  /** Grouping weight inside a category; lower sorts first. */
  readonly order: number;
  /** Show this row in the ⌘K palette as well as the shortcuts dialog. */
  readonly inPalette?: boolean;
}

/**
 * The canonical command registry. Binding strings in `display` are illustrative
 * only — always render {@link formatBinding} so the glyphs match the user's OS.
 */
export const SHORTCUT_COMMANDS: readonly ShortcutCommand[] = [
  {
    id: "palette.open",
    label: "Open command palette",
    description: "Search every command, agent, case and past investigation.",
    binding: { key: "k", mod: true },
    category: "Navigation",
    order: 0,
    inPalette: true,
  },
  {
    id: "view.graph",
    label: "Go to graph",
    description: "Focus the knowledge-graph view.",
    binding: { key: "1" },
    category: "Navigation",
    order: 1,
    inPalette: true,
  },
  {
    id: "view.timeline",
    label: "Go to timeline",
    description: "Focus the reconstructed timeline.",
    binding: { key: "2" },
    category: "Navigation",
    order: 2,
    inPalette: true,
  },
  {
    id: "view.reports",
    label: "Go to reports",
    description: "Focus generated intelligence reports.",
    binding: { key: "3" },
    category: "Navigation",
    order: 3,
    inPalette: true,
  },
  {
    id: "view.explain",
    label: "Explain selected entity",
    description: "Open the reasoning trace behind the current selection.",
    binding: { key: "e", mod: true },
    category: "Navigation",
    order: 4,
    inPalette: true,
  },
  {
    id: "investigation.focus",
    label: "Focus the input bar",
    description: "Jump to the investigation input field.",
    binding: { key: "/", allowInInput: true },
    category: "Investigation",
    order: 0,
    inPalette: true,
  },
  {
    id: "investigation.run",
    label: "Run investigation",
    description: "Submit the current input to the agent swarm.",
    binding: { key: "enter", mod: true },
    category: "Investigation",
    order: 1,
    inPalette: true,
  },
  {
    id: "investigation.new",
    label: "New investigation",
    description: "Clear the current run and start a fresh one.",
    binding: { key: "r", mod: true },
    category: "Investigation",
    order: 2,
    inPalette: true,
  },
  {
    id: "view.toggleEntities",
    label: "Cycle result density",
    description: "Switch between compact and expanded result rows.",
    binding: { key: "d", mod: true, shift: true },
    category: "View",
    order: 0,
    inPalette: true,
  },
  {
    id: "theme.toggle",
    label: "Toggle theme",
    description: "Switch between the dark console and the light variant.",
    binding: { key: "t", mod: true },
    category: "View",
    order: 1,
    inPalette: true,
  },
  {
    id: "edit.copy",
    label: "Copy report markdown",
    description: "Copy the current report to the clipboard.",
    binding: { key: "c", mod: true, shift: true },
    category: "Editing",
    order: 0,
    inPalette: true,
  },
  {
    id: "edit.export",
    label: "Export evidence bundle",
    description: "Download the case export (JSON + Markdown).",
    binding: { key: "e", mod: true, shift: true },
    category: "Editing",
    order: 1,
    inPalette: true,
  },
  {
    id: "shortcuts.open",
    label: "Show keyboard shortcuts",
    description: "Open this reference dialog.",
    binding: { key: "?", shift: true },
    category: "System",
    order: 0,
    inPalette: true,
  },
  {
    id: "onboarding.restart",
    label: "Replay the tour",
    description: "Re-open the first-run walkthrough.",
    binding: { key: "?", mod: true, shift: true },
    category: "System",
    order: 1,
    inPalette: true,
  },
  {
    id: "dialog.close",
    label: "Close dialog",
    description: "Dismiss the top-most dialog or palette.",
    binding: { key: "escape" },
    category: "System",
    order: 2,
    inPalette: true,
  },
] as const;

/**
 * Bare single-key bindings that select an agent by its 1-based position in
 * {@link import("./agentCatalog").AGENT_CATALOG}. Only active while no text
 * field has focus, and deliberately limited to 1–9.
 */
export const AGENT_INDEX_KEYS: readonly string[] = [
  "1", "2", "3", "4", "5", "6", "7", "8", "9",
] as const;

/** Is this an Apple-platform browser? */
export function isApplePlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const platform =
    (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData
      ?.platform ??
    navigator.platform ??
    navigator.userAgent ??
    "";
  return /mac|iphone|ipad|ipod/i.test(platform);
}

/**
 * Resolve a {@link BindingSpec} into a concrete {@link KeyBinding} for the
 * current platform. `mod` becomes `metaKey` on Apple, `ctrlKey` elsewhere.
 */
export function resolveBinding(spec: BindingSpec, apple = isApplePlatform()): KeyBinding {
  const mod = spec.mod === true;
  const metaKey = spec.meta === true || (mod && apple);
  const ctrlKey = spec.ctrl === true || (mod && !apple);
  return {
    key: normalizeKey(spec.key),
    metaKey,
    ctrlKey,
    shiftKey: spec.shift === true,
    altKey: spec.alt === true,
  };
}

/** Lower-case a single character; leave named keys alone. */
export function normalizeKey(key: string): string {
  return key.length === 1 ? key.toLowerCase() : key.toLowerCase();
}

/** Parse a binding string like `"mod+k"`, `"shift+?"`, `"escape"`, `"mod+shift+e"`. */
export function parseBinding(source: string): BindingSpec {
  const parts = source
    .split("+")
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length === 0) return { key: "" };
  const key = parts[parts.length - 1];
  const spec: {
    key: string;
    mod?: boolean;
    ctrl?: boolean;
    meta?: boolean;
    shift?: boolean;
    alt?: boolean;
    allowInInput?: boolean;
  } = { key };
  for (const part of parts.slice(0, -1)) {
    switch (part.toLowerCase()) {
      case "mod":
      case "cmdorctrl":
        spec.mod = true;
        break;
      case "ctrl":
      case "control":
        spec.ctrl = true;
        break;
      case "meta":
      case "cmd":
      case "command":
        spec.meta = true;
        break;
      case "shift":
        spec.shift = true;
        break;
      case "alt":
      case "option":
        spec.alt = true;
        break;
      default:
        // Unknown modifier: treated as part of the key rather than silently dropped.
        spec.key = `${part}+${key}`;
        return spec;
    }
  }
  return spec;
}

/** Does this event satisfy a resolved binding? */
export function matchesEvent(event: KeyEventLike, binding: KeyBinding): boolean {
  if (normalizeKey(event.key) !== binding.key) return false;
  if (Boolean(event.metaKey) !== Boolean(binding.metaKey)) return false;
  if (Boolean(event.ctrlKey) !== Boolean(binding.ctrlKey)) return false;
  if (Boolean(event.shiftKey) !== Boolean(binding.shiftKey)) return false;
  if (Boolean(event.altKey) !== Boolean(binding.altKey)) return false;
  return true;
}

/**
 * Does this event match the binding described by `spec`, resolving `mod` for
 * the detected platform?
 */
export function eventMatchesSpec(
  event: KeyEventLike,
  spec: BindingSpec,
  apple = isApplePlatform(),
): boolean {
  return matchesEvent(event, resolveBinding(spec, apple));
}

/** True when the event target is a text-entry surface. */
export function isEditableTarget(target: EventTarget | null | undefined): boolean {
  if (!target || typeof (target as Element).tagName !== "string") return false;
  const element = target as HTMLElement;
  const tag = element.tagName.toUpperCase();
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (element.isContentEditable) return true;
  return false;
}

/**
 * True when a shortcut must be suppressed because the user is typing.
 * `allowInInput` overrides this per command (⌘K still works in a text field).
 */
export function shouldSuppressForFocus(
  event: KeyEventLike,
  spec: BindingSpec,
): boolean {
  if (spec.allowInInput === true) return false;
  if (!isEditableTarget(event.target)) return false;
  // Modified shortcuts (⌘K, ⌘E) stay live inside inputs — that is the whole
  // point of a command palette. Bare "/" and "?" must not hijack typing.
  return !(event.metaKey || event.ctrlKey);
}

/** Render a binding as display glyphs for the current platform. */
export function formatBinding(spec: BindingSpec, apple = isApplePlatform()): string {
  const resolved = resolveBinding(spec, apple);
  // Canonical modifier order: ⌃ ⌥ ⇧ ⌘ (Ctrl, Alt, Shift, Cmd).
  const parts: string[] = [];
  if (resolved.ctrlKey) parts.push(apple ? "⌃" : "Ctrl");
  if (resolved.altKey) parts.push(apple ? "⌥" : "Alt");
  if (resolved.shiftKey) parts.push(apple ? "⇧" : "Shift");
  if (resolved.metaKey) parts.push(apple ? "⌘" : "Ctrl");

  const key = resolved.key;
  if (key === "enter") parts.push(apple ? "↩" : "Enter");
  else if (key === "escape") parts.push("Esc");
  else if (key === "arrowup") parts.push("↑");
  else if (key === "arrowdown") parts.push("↓");
  else if (key === "arrowleft") parts.push("←");
  else if (key === "arrowright") parts.push("→");
  else if (key === " ") parts.push("Space");
  else if (key === "backspace") parts.push("⌫");
  else if (key.length === 1) parts.push(key.toUpperCase());
  else parts.push(key.charAt(0).toUpperCase() + key.slice(1));

  return apple ? parts.join("") : parts.join("+");
}

/** Commands grouped by category, in registry order. */
export function commandsByCategory(): ReadonlyArray<{
  category: ShortcutCategory;
  commands: readonly ShortcutCommand[];
}> {
  return SHORTCUT_CATEGORIES.map((category) => ({
    category,
    commands: SHORTCUT_COMMANDS.filter((c) => c.category === category).sort(
      (a, b) => a.order - b.order,
    ),
  })).filter((group) => group.commands.length > 0);
}

/** Look up a command by id. */
export function commandById(id: CommandId | string | null | undefined): ShortcutCommand | undefined {
  if (!id) return undefined;
  return SHORTCUT_COMMANDS.find((c) => c.id === id);
}

/** Handler signature for a fired command. */
export type HotkeyHandler = (event: KeyboardEvent) => void;

/**
 * A binding + its handler, as consumed by {@link useHotkeys}. Pass
 * `binding` as an authored string ("mod+k") or a {@link BindingSpec}.
 */
export interface HotkeyBinding {
  /** Authored binding, e.g. `"mod+shift+e"`. */
  readonly binding: string | BindingSpec;
  readonly handler: HotkeyHandler;
  /** Defaults to the spec's `allowInInput` (false). */
  readonly allowInInput?: boolean;
  /** When true, `event.preventDefault()` runs only if this returns true. */
  readonly shouldPreventDefault?: boolean;
}

/**
 * Bind a set of hotkeys to the window for the lifetime of the component.
 *
 * Guards, in order:
 *   - disabled entirely when `enabled` is false;
 *   - suppressed while a text field has focus, unless the binding opts in
 *     (a bare `/` must not type a slash; ⌘K must still open the palette);
 *   - autorepeat ignored for commands that are not held-key navigation;
 *   - `preventDefault()` only when a handler actually fires, so unclaimed keys
 *     keep their native behaviour.
 */
export function useHotkeys(
  bindings: readonly HotkeyBinding[],
  enabled = true,
): void {
  // Keep the latest bindings in a ref so callers may pass a fresh array each
  // render without re-attaching the listener.
  const ref = useRef(bindings);
  ref.current = bindings;

  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;

    const onKeyDown = (event: KeyboardEvent) => {
      const apple = isApplePlatform();
      const current = ref.current;
      if (current.length === 0) return;

      const like: KeyEventLike = {
        key: event.key,
        metaKey: event.metaKey,
        ctrlKey: event.ctrlKey,
        shiftKey: event.shiftKey,
        altKey: event.altKey,
        repeat: event.repeat,
        target: event.target,
      };

      for (const entry of current) {
        const spec: BindingSpec =
          typeof entry.binding === "string"
            ? parseBinding(entry.binding)
            : entry.binding;
        const effective: BindingSpec =
          entry.allowInInput === undefined
            ? spec
            : { ...spec, allowInInput: entry.allowInInput };

        if (!eventMatchesSpec(like, spec, apple)) continue;
        if (shouldSuppressForFocus(like, effective)) continue;

        const shouldPrevent =
          entry.shouldPreventDefault === undefined
            ? Boolean(spec.mod) || Boolean(spec.meta) || Boolean(spec.ctrl)
            : entry.shouldPreventDefault;
        if (shouldPrevent) event.preventDefault();
        entry.handler(event);
        return;
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}

/**
 * Convenience: bind a {@link SHORTCUT_COMMANDS} entry straight to a callback.
 * Returns a stable factory so a component can map ids → handlers inline.
 */
export function hotkeyFor(
  id: CommandId,
  handler: HotkeyHandler,
  overrides?: Partial<ShortcutCommand>,
): HotkeyBinding {
  const command = commandById(id);
  return {
    binding: command?.binding ?? { key: "" },
    handler,
    allowInInput: overrides?.binding?.allowInInput,
    shouldPreventDefault: Boolean(command?.binding.mod ?? command?.binding.meta),
  };
}