import { memo, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";
import { normalizeMath } from "../lib/math";

// One renderer for every place Ember's replies are shown (chat bubbles and the HUD): GitHub-flavoured Markdown
// plus LaTeX maths typeset by KaTeX. The fonts are bundled, so it works offline inside Electron.
const REMARK = [remarkGfm, remarkMath];
const REHYPE: [typeof rehypeKatex, { throwOnError: boolean; strict: boolean; errorColor: string }][] = [
  [rehypeKatex, { throwOnError: false, strict: false, errorColor: "#fca5a5" }],
];

export const Markdown = memo(function Markdown({ children }: { children: string }) {
  const text = useMemo(() => normalizeMath(children), [children]);
  return (
    <ReactMarkdown remarkPlugins={REMARK} rehypePlugins={REHYPE}>
      {text}
    </ReactMarkdown>
  );
});
