"use client";

/* Authenticated asset thumbnails intentionally use their same-origin URLs. */
/* eslint-disable @next/next/no-img-element */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export type PromptMentionItem = {
  id: string;
  label: string;
  kind: "image" | "video" | "audio";
  previewUrl?: string;
  connected: boolean;
};

type PromptMentionComposerProps = {
  value: string;
  onChange: (value: string) => void;
  items: PromptMentionItem[];
  onSelectItem: (item: PromptMentionItem) => boolean | Promise<boolean>;
  placeholder: string;
  ariaLabel: string;
  id?: string;
  onFocus?: () => void;
  disabled?: boolean;
};

const TOKEN_PATTERN = /@\{([^}\n]+)\}/g;

export function promptMentionToken(assetId: string): string {
  return `@{${assetId}}`;
}

function itemMedia(item: PromptMentionItem): HTMLElement {
  if (item.previewUrl) {
    const image = document.createElement("img");
    image.src = item.previewUrl;
    image.alt = "";
    return image;
  }
  const icon = document.createElement("i");
  icon.textContent = item.kind === "video" ? "▶" : item.kind === "audio" ? "♪" : "▧";
  return icon;
}

function createToken(item: PromptMentionItem): HTMLSpanElement {
  const token = document.createElement("span");
  token.className = "prompt-mention-token";
  token.contentEditable = "false";
  token.dataset.promptMention = promptMentionToken(item.id);
  token.dataset.assetId = item.id;
  token.title = `${item.label} · ${item.id}`;
  token.append(itemMedia(item));
  const label = document.createElement("b");
  label.textContent = item.label;
  token.append(label);
  return token;
}

function serializeEditor(root: HTMLElement): string {
  const visit = (node: Node): string => {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
    if (!(node instanceof HTMLElement)) return "";
    const token = node.dataset.promptMention;
    if (token) return token;
    if (node.tagName === "BR") return "\n";
    const value = Array.from(node.childNodes).map(visit).join("");
    return node !== root && node.tagName === "DIV" ? `${value}\n` : value;
  };
  return Array.from(root.childNodes).map(visit).join("").replace(/\n$/, "");
}

function replaceEditor(root: HTMLElement, value: string, byId: Map<string, PromptMentionItem>) {
  const fragment = document.createDocumentFragment();
  let cursor = 0;
  for (const match of value.matchAll(TOKEN_PATTERN)) {
    const index = match.index ?? 0;
    if (index > cursor) fragment.append(document.createTextNode(value.slice(cursor, index)));
    const item = byId.get(match[1]);
    fragment.append(item ? createToken(item) : document.createTextNode(match[0]));
    cursor = index + match[0].length;
  }
  if (cursor < value.length) fragment.append(document.createTextNode(value.slice(cursor)));
  root.replaceChildren(fragment);
}

export default function PromptMentionComposer({ value, onChange, items, onSelectItem, placeholder, ariaLabel, id, onFocus, disabled = false }: PromptMentionComposerProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const rangeRef = useRef<Range | undefined>(undefined);
  const lastValueRef = useRef("");
  const lastItemsRef = useRef("");
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [selecting, setSelecting] = useState(false);
  const byId = useMemo(() => new Map(items.map((item) => [item.id, item])), [items]);
  // Connection status only changes the picker grouping. Rebuilding the editor
  // for that change discards its live caret, so a second @ can land at the start.
  const itemsKey = useMemo(() => items.map((item) => `${item.id}:${item.label}:${item.kind}:${item.previewUrl ?? ""}`).join("|"), [items]);
  const filtered = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return query ? items.filter((item) => `${item.label} ${item.kind}`.toLocaleLowerCase().includes(query)) : items;
  }, [items, search]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root || (lastValueRef.current === value && lastItemsRef.current === itemsKey)) return;
    replaceEditor(root, value, byId);
    lastValueRef.current = value;
    lastItemsRef.current = itemsKey;
  }, [byId, itemsKey, value]);

  useEffect(() => {
    if (!open) return;
    searchRef.current?.focus();
    const close = (event: PointerEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);

  const rememberRange = useCallback(() => {
    const root = rootRef.current;
    const selection = window.getSelection();
    if (root && selection?.rangeCount) {
      const range = selection.getRangeAt(0);
      if (root.contains(range.commonAncestorContainer)) {
        rangeRef.current = range.cloneRange();
        return;
      }
    }
    if (root) {
      const range = document.createRange();
      range.selectNodeContents(root);
      range.collapse(false);
      rangeRef.current = range;
    }
  }, []);

  const openPicker = useCallback(() => {
    if (disabled) return;
    rememberRange();
    setSearch("");
    setOpen(true);
  }, [disabled, rememberRange]);

  const emit = useCallback(() => {
    const root = rootRef.current;
    if (!root) return;
    const next = serializeEditor(root);
    lastValueRef.current = next;
    onChange(next);
  }, [onChange]);

  const insert = useCallback(async (item: PromptMentionItem) => {
    if (selecting) return;
    setSelecting(true);
    try {
      if (!await onSelectItem(item)) return;
      const root = rootRef.current;
      if (!root) return;
      root.focus();
      const selection = window.getSelection();
      const range = rangeRef.current ?? document.createRange();
      if (!rangeRef.current) {
        range.selectNodeContents(root);
        range.collapse(false);
      }
      range.deleteContents();
      const token = createToken({ ...item, connected: true });
      const spacer = document.createTextNode(" ");
      range.insertNode(spacer);
      range.insertNode(token);
      range.setStartAfter(spacer);
      range.collapse(true);
      selection?.removeAllRanges();
      selection?.addRange(range);
      rangeRef.current = range.cloneRange();
      emit();
      setOpen(false);
    } finally {
      setSelecting(false);
    }
  }, [emit, onSelectItem, selecting]);

  const groups = [
    { key: "connected", label: "已引用", values: filtered.filter((item) => item.connected) },
    { key: "library", label: "素材引用", values: filtered.filter((item) => !item.connected) },
  ];

  return <div className="prompt-mention-composer" ref={wrapperRef} onPointerDown={(event) => event.stopPropagation()}>
    <div className="prompt-mention-input-row">
      <div
        id={id}
        ref={rootRef}
        className="prompt-mention-editor"
        contentEditable={!disabled}
        suppressContentEditableWarning
        role="textbox"
        tabIndex={0}
        aria-label={ariaLabel}
        aria-multiline="true"
        data-placeholder={placeholder}
        onFocus={onFocus}
        onInput={emit}
        onBlur={rememberRange}
        onKeyDown={(event) => {
          if (event.key === "@") { event.preventDefault(); openPicker(); return; }
          if (event.key === "Escape" && open) { event.preventDefault(); setOpen(false); return; }
          if (event.key === "Enter" && !open) {
            event.preventDefault();
            document.execCommand("insertText", false, "\n");
            emit();
          }
        }}
        onPaste={(event) => {
          event.preventDefault();
          document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
          emit();
        }}
      />
      <button type="button" className="prompt-mention-trigger" aria-label="引用素材" aria-expanded={open} disabled={disabled} onMouseDown={(event) => event.preventDefault()} onClick={openPicker}>@</button>
    </div>
    {open && <div className="prompt-mention-picker" role="dialog" aria-label="选择提示词参考素材">
      <label><span aria-hidden="true">⌕</span><input ref={searchRef} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索素材" onKeyDown={(event) => { if (event.key === "Escape") setOpen(false); }}/></label>
      <div className="prompt-mention-scroll">{groups.map((group) => group.values.length ? <section key={group.key}><strong>{group.label}</strong>{group.values.map((item) => <button key={`${group.key}-${item.id}`} type="button" disabled={selecting} onMouseDown={(event) => event.preventDefault()} onClick={() => void insert(item)}>{item.previewUrl ? <img src={item.previewUrl} alt="" loading="lazy" decoding="async"/> : <i>{item.kind === "video" ? "▶" : item.kind === "audio" ? "♪" : "▧"}</i>}<span><b>{item.label}</b><small>{item.kind === "image" ? "图片" : item.kind === "video" ? "视频" : "音频"}{item.connected ? " · 已引用" : " · 选择后加入参考"}</small></span></button>)}</section> : null)}{!filtered.length && <p>没有匹配的素材；请先从左侧资产上传。</p>}</div>
    </div>}
  </div>;
}
