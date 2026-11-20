/* Minimal Markdown for agent answers, rendered as React elements (never innerHTML: the text comes from a model).
 * Supports headings, paragraphs, lists, fenced code (with copy), inline code, bold, italic, links (http/https
 * only). Inline code that looks like a project path (`src/app.py:42`) opens the file at that line. */
import { Fragment, useState, type ReactNode } from "react";
import { Check, Copy } from "lucide-react";
import { editors } from "../lib/editors";

const PATH_RE = /^[\w.@/-]+\.[A-Za-z0-9]{1,8}(?::(\d+))?$/;

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\((https?:\/\/[^)\s]+)\))/g;
  let last = 0, m: RegExpExecArray | null, i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = `${key}-${i++}`;
    if (m[1]) {
      const code = m[1].slice(1, -1);
      const pm = code.match(PATH_RE);
      if (pm && (code.includes("/") || /\.(py|ts|tsx|js|jsx|md|json|toml|yml|yaml|go|rs|java|c|cpp|h|rb|php|sh|css|html|txt)(:\d+)?$/.test(code))) {
        const [path, line] = code.split(":");
        out.push(<button key={k} type="button" className="md-path" title={code}
                         onClick={() => void editors.open(path!, { line: line ? Number(line) : undefined })}>{code}</button>);
      } else out.push(<code key={k}>{code}</code>);
    } else if (m[2]) out.push(<strong key={k}>{inline(m[2].slice(2, -2), k)}</strong>);
    else if (m[3]) out.push(<em key={k}>{m[3].slice(1, -1)}</em>);
    else if (m[4]) out.push(<a key={k} href={m[5]} target="_blank" rel="noopener noreferrer">{m[4].slice(1, m[4].indexOf("]"))}</a>);
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function CodeBlock({ code, lang }: { code: string; lang: string }) {
  const [done, setDone] = useState(false);
  return (
    <div className="md-code">
      <div className="md-code-h"><span>{lang || "code"}</span>
        <button type="button" onClick={() => { void navigator.clipboard?.writeText(code).then(() => { setDone(true); window.setTimeout(() => setDone(false), 1400); }); }}
                aria-label="Copy">{done ? <Check size={12} /> : <Copy size={12} />}</button>
      </div>
      <pre><code>{code}</code></pre>
    </div>
  );
}

export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0, n = 0;
  while (i < lines.length) {
    const line = lines[i]!;
    const key = `b${n++}`;
    const fence = line.match(/^\s*```(\w*)/);
    if (fence) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i]!)) body.push(lines[i++]!);
      i++;
      blocks.push(<CodeBlock key={key} code={body.join("\n")} lang={fence[1] ?? ""} />);
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { const L = Math.min(4, h[1]!.length + 2); const Tag = `h${L}` as "h3"; blocks.push(<Tag key={key}>{inline(h[2]!, key)}</Tag>); i++; continue; }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i]!)) items.push(lines[i++]!.replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
      const List = ordered ? "ol" : "ul";
      blocks.push(<List key={key}>{items.map((it, j) => <li key={j}>{inline(it, `${key}-${j}`)}</li>)}</List>);
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const para: string[] = [];
    while (i < lines.length && lines[i]!.trim() && !/^\s*```/.test(lines[i]!) && !/^#{1,4}\s/.test(lines[i]!) && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]!)) para.push(lines[i++]!);
    blocks.push(<p key={key}>{para.map((p, j) => <Fragment key={j}>{j > 0 && <br />}{inline(p, `${key}-${j}`)}</Fragment>)}</p>);
  }
  return <div className="md">{blocks}</div>;
}
