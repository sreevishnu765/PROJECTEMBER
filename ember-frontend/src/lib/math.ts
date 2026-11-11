// Prepares an assistant reply for remark-math. Ember's model writes maths in whichever LaTeX style it likes
// ($...$, $$...$$, \(...\), \[...\]); remark-math only understands the dollar forms, and treats any two
// dollar signs on a line as maths, so "it costs $5 and $10" would be typeset as the expression "5 and ".
// This normalises the delimiters and protects ordinary dollar amounts, leaving code (fenced or inline) alone.

const CODE_RE = /(```[\s\S]*?(?:```|$)|`[^`\n]*`)/g; // an unterminated fence still counts: replies stream in

// Something that is really maths, not a price: a LaTeX command, a super/subscript or brace, or a relation/operator
// next to a letter (x = 5, a + b). "5 and " and "10 each" are not.
const LATEXISH = /\\[A-Za-z]+|[\^_{}]|[A-Za-z]\s*[=<>+\-*/]|[=<>]\s*[A-Za-z0-9]/;

function fixPlain(text: string): string {
  // \[ ... \]  ->  $$ ... $$ (display)      \( ... \)  ->  $ ... $ (inline)
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (_m, body: string) => `\n\n$$\n${body.trim()}\n$$\n\n`);
  text = text.replace(/\\\(([\s\S]+?)\\\)/g, (_m, body: string) => `$${body.trim()}$`);

  // $...$ pairs that are not maths (prices) get their dollar signs escaped so they print as typed.
  text = text.replace(/(?<![\\$])\$(?!\$)([^$\n]+?)\$(?!\$)/g, (m, body: string) =>
    LATEXISH.test(body) ? m : `\\$${body}\\$`,
  );

  // A line that is nothing but one formula is displayed (centred, full size) rather than squeezed inline.
  return text
    .split("\n")
    .map((line) => {
      const m = /^\s*\$([^$\n]+)\$\s*$/.exec(line);
      return m && LATEXISH.test(m[1]) ? `\n$$\n${m[1].trim()}\n$$\n` : line;
    })
    .join("\n");
}

export function normalizeMath(source: string): string {
  if (!source || (!source.includes("\\") && !source.includes("$"))) return source;
  return source
    .split(CODE_RE)
    .map((part, i) => (i % 2 === 1 ? part : fixPlain(part)))
    .join("");
}
