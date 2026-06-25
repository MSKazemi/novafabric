import { createHighlighter, type Highlighter, type BundledLanguage } from 'shiki';

let highlighterPromise: Promise<Highlighter> | null = null;

const LANGUAGES: BundledLanguage[] = [
  'bash',
  'shell',
  'json',
  'yaml',
  'python',
  'typescript',
  'javascript',
  'tsx',
  'jsx',
  'diff',
];

export async function getHighlighter(): Promise<Highlighter> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighter({
      themes: ['github-dark-default', 'github-light-default'],
      langs: LANGUAGES,
    });
  }
  return highlighterPromise;
}

export async function highlight(code: string, lang: BundledLanguage = 'bash'): Promise<string> {
  const hi = await getHighlighter();
  return hi.codeToHtml(code, {
    lang,
    themes: {
      dark: 'github-dark-default',
      light: 'github-light-default',
    },
    defaultColor: false,
  });
}
