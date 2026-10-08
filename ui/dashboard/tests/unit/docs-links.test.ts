import { describe, expect, it } from 'vitest';
import { rewriteLinks } from '../../src/lib/docs';

const published = new Set(['concepts', 'ops/monitoring', 'rfcs']);

function rewrite(href: string, file = 'cli-reference.md'): string {
  return rewriteLinks(`<a href="${href}">x</a>`, file, published);
}

describe('docs relative-link rewriting', () => {
  it('keeps published Markdown docs on novafabric.ai, including anchors', () => {
    expect(rewrite('concepts.md#replay')).toContain('href="/docs/concepts/#replay"');
    expect(rewrite('../concepts.md', 'ops/monitoring.md')).toContain(
      'href="/docs/concepts/"',
    );
  });

  it('sends excluded Markdown docs to the GitHub blob', () => {
    expect(rewrite('releases/v0.10.0.md')).toContain(
      'href="https://github.com/MSKazemi/novafabric/blob/main/docs/releases/v0.10.0.md"',
    );
  });

  it('sends non-Markdown files inside docs to the GitHub blob', () => {
    expect(rewrite('api/openapi.yaml')).toContain(
      'href="https://github.com/MSKazemi/novafabric/blob/main/docs/api/openapi.yaml"',
    );
    expect(rewrite('assets/brand/novafabric-mark.svg')).toContain(
      'href="https://github.com/MSKazemi/novafabric/blob/main/docs/assets/brand/novafabric-mark.svg"',
    );
  });

  it('resolves files that escape docs against the repository root', () => {
    expect(rewrite('../CITATION.cff')).toContain(
      'href="https://github.com/MSKazemi/novafabric/blob/main/CITATION.cff"',
    );
    expect(rewrite('../../schemas/run-capsule.schema.json', 'ops/monitoring.md')).toContain(
      'href="https://github.com/MSKazemi/novafabric/blob/main/schemas/run-capsule.schema.json"',
    );
  });

  it('routes directory targets to GitHub tree URLs', () => {
    expect(rewrite('releases/')).toContain(
      'href="https://github.com/MSKazemi/novafabric/tree/main/docs/releases"',
    );
    expect(rewrite('../deploy/hpc/')).toContain(
      'href="https://github.com/MSKazemi/novafabric/tree/main/deploy/hpc"',
    );
  });

  it('maps a bare docs-root link to /docs/', () => {
    expect(rewrite('.')).toContain('href="/docs/"');
  });

  it('leaves absolute, fragment-only, and query-only links untouched', () => {
    expect(rewrite('https://example.com/a')).toBe(
      '<a href="https://example.com/a">x</a>',
    );
    expect(rewrite('#local')).toBe('<a href="#local">x</a>');
    expect(rewrite('/install/')).toBe('<a href="/install/">x</a>');
    expect(rewrite('?view=all')).toBe('<a href="?view=all">x</a>');
  });
});
