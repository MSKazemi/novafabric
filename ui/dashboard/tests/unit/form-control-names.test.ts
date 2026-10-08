/**
 * Regression guard: every form control in the audited dashboard tabs has an
 * accessible name.
 *
 * Static JSX scan (no rendering, so conditionally-shown controls are covered
 * too). A control is <input>, <textarea>, <select> or the Input / Select /
 * Textarea / SuggestInput wrappers. It is named when it has a non-empty
 * aria-label / aria-labelledby / title, sits inside a <label> or a <Field
 * label=...>, or has an id matched by a <label htmlFor> in the same file.
 * A visually adjacent <label> WITHOUT htmlFor does not name anything.
 */
import { parse } from '@babel/parser';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { describe, expect, it } from 'vitest';

const TABS = join(__dirname, '../../src/components/dashboard/tabs');
const SCOPES = ['Analytics', 'Cost', 'Governance', 'Policy', 'Infra', 'Seal', 'Audit', 'Lineage', 'Commands'];
const CONTROLS = new Set(['input', 'textarea', 'select', 'Input', 'Select', 'Textarea', 'SuggestInput']);

function files(): string[] {
  const out: string[] = [];
  const walk = (p: string) => {
    if (statSync(p).isDirectory()) readdirSync(p).forEach((c) => walk(join(p, c)));
    else if (p.endsWith('.tsx')) out.push(p);
  };
  for (const name of readdirSync(TABS)) {
    const full = join(TABS, name);
    const base = name.replace(/\.tsx$/, '').replace(/Tab$/, '');
    const dir = statSync(full).isDirectory() ? name.toLowerCase() : base.toLowerCase();
    if (SCOPES.some((s) => s.toLowerCase() === dir)) walk(full);
  }
  return out.sort();
}

type N = any; // Babel AST nodes; loosely typed on purpose.

function tagName(el: N): string {
  const n = el.openingElement.name;
  return n.type === 'JSXIdentifier' ? n.name : '';
}
function attr(el: N, name: string): N | undefined {
  return el.openingElement.attributes.find((a: N) => a.type === 'JSXAttribute' && a.name.name === name);
}
function attrText(a: N | undefined, src: string): string | null {
  if (!a) return null;
  if (!a.value) return '';
  if (a.value.type === 'StringLiteral') return a.value.value;
  return src.slice(a.value.start, a.value.end); // expression text, e.g. {id}
}
function nonEmpty(a: N | undefined): boolean {
  if (!a) return false;
  if (a.value?.type === 'StringLiteral') return a.value.value.trim() !== '';
  return !!a.value; // dynamic expression: trust it
}

export function unnamedControls(src: string): Array<{ line: number; tag: string }> {
  const ast = parse(src, { sourceType: 'module', plugins: ['jsx', 'typescript'] });
  const labelFor = new Set<string>();
  const found: Array<{ line: number; tag: string }> = [];

  const visit = (node: N, ancestors: N[]) => {
    if (!node || typeof node.type !== 'string') return;
    if (node.type === 'JSXElement') {
      const tag = tagName(node);
      if (tag === 'label') {
        const t = attrText(attr(node, 'htmlFor'), src);
        if (t) labelFor.add(t);
      }
      if (CONTROLS.has(tag)) found.push({ line: node.loc.start.line, tag, node, ancestors } as N);
    }
    for (const key of Object.keys(node)) {
      if (key === 'loc' || key === 'start' || key === 'end') continue;
      const v = node[key];
      const next = node.type === 'JSXElement' ? [...ancestors, node] : ancestors;
      if (Array.isArray(v)) v.forEach((c) => visit(c, next));
      else if (v && typeof v === 'object') visit(v, next);
    }
  };
  visit(ast.program, []);

  return (found as N[])
    .filter(({ node, ancestors }) => {
      const type = attrText(attr(node, 'type'), src);
      if (type === 'hidden') return false;
      if (nonEmpty(attr(node, 'aria-label')) || nonEmpty(attr(node, 'aria-labelledby')) || nonEmpty(attr(node, 'title'))) return false;
      if (ancestors.some((a: N) => tagName(a) === 'label' || (tagName(a) === 'Field' && attr(a, 'label')))) return false;
      const id = attrText(attr(node, 'id'), src);
      if (id && labelFor.has(id)) return false;
      return true;
    })
    .map(({ line, tag }) => ({ line, tag }));
}

describe('scanner self-test', () => {
  const wrap = (body: string) => `export const X = () => (<div>${body}</div>);`;
  it('flags unnamed controls', () => {
    expect(unnamedControls(wrap('<label>Name</label><input />'))).toHaveLength(1);
    expect(unnamedControls(wrap('<select><option/></select>'))).toHaveLength(1);
    expect(unnamedControls(wrap('<SuggestInput value="" />'))).toHaveLength(1);
    expect(unnamedControls(wrap('<input aria-label="" />'))).toHaveLength(1);
  });
  it('accepts named controls', () => {
    expect(unnamedControls(wrap('<input aria-label="Run id" />'))).toHaveLength(0);
    expect(unnamedControls(wrap('<label>Name <input /></label>'))).toHaveLength(0);
    expect(unnamedControls(wrap('<label htmlFor="a">A</label><textarea id="a" />'))).toHaveLength(0);
    expect(unnamedControls(wrap('<input type="hidden" />'))).toHaveLength(0);
  });
});

describe('dashboard tab form controls have accessible names', () => {
  const list = files();
  it('scans a non-trivial set of files', () => {
    expect(list.length).toBeGreaterThan(9);
  });
  it.each(list.map((f) => [relative(TABS, f), f]))('%s', (_rel, f) => {
    const bad = unnamedControls(readFileSync(f, 'utf8'));
    expect(bad.map((b) => `line ${b.line}: <${b.tag}> has no accessible name`)).toEqual([]);
  });
});
