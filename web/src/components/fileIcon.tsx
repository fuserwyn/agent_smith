/* File-type icons: a lucide glyph + a colour per family, like an editor's file explorer. */
import { Braces, File, FileBraces, FileCode, FileImage, FileLock, FileText, Folder, FolderOpen, Hash } from "lucide-react";

const CODE: Record<string, string> = {
  py: "#4b8bbe", ts: "#3178c6", tsx: "#3178c6", js: "#e5c04b", jsx: "#e5c04b", mjs: "#e5c04b", cjs: "#e5c04b",
  go: "#00add8", rs: "#dea584", java: "#b07219", kt: "#a97bff", c: "#9aa4b5", h: "#9aa4b5", cpp: "#f34b7d", hpp: "#f34b7d",
  rb: "#cc342d", php: "#777bb4", swift: "#f05138", sh: "#89e051", bash: "#89e051", zsh: "#89e051", html: "#e34c26",
  css: "#563d7c", scss: "#c6538c", vue: "#41b883", svelte: "#ff3e00", sql: "#e38c00", lua: "#000080",
};
const DATA = new Set(["json", "jsonl", "yaml", "yml", "toml", "ini", "cfg", "lock", "xml", "csv"]);
const TEXT = new Set(["md", "txt", "rst", "log", "adoc"]);
const IMG = new Set(["png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp"]);

export function FileIcon({ name, dir, open, sensitive }: { name: string; dir?: boolean; open?: boolean; sensitive?: boolean }) {
  const s = 15, w = 1.7;
  if (dir) return open ? <FolderOpen size={s} strokeWidth={w} className="fi dir" /> : <Folder size={s} strokeWidth={w} className="fi dir" />;
  if (sensitive) return <FileLock size={s} strokeWidth={w} className="fi" style={{ color: "var(--warn)" }} />;
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  if (name === "Makefile" || name === "Dockerfile") return <Hash size={s} strokeWidth={w} className="fi" style={{ color: "#89e051" }} />;
  if (CODE[ext]) return <FileCode size={s} strokeWidth={w} className="fi" style={{ color: CODE[ext] }} />;
  if (ext === "json") return <FileBraces size={s} strokeWidth={w} className="fi" style={{ color: "#e5c04b" }} />;
  if (DATA.has(ext)) return <Braces size={s} strokeWidth={w} className="fi" style={{ color: "#cb7bd6" }} />;
  if (TEXT.has(ext)) return <FileText size={s} strokeWidth={w} className="fi" style={{ color: "#6ea8fe" }} />;
  if (IMG.has(ext)) return <FileImage size={s} strokeWidth={w} className="fi" style={{ color: "#3fb950" }} />;
  return <File size={s} strokeWidth={w} className="fi" />;
}
