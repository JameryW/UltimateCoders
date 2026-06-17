import {describe, it, expect} from 'vitest';
import {renderMarkdown, hasMarkdown} from './markdown.js';

describe('markdown', () => {
  describe('hasMarkdown', () => {
    it('detects headings', () => {
      expect(hasMarkdown('# Title')).toBe(true);
      expect(hasMarkdown('## Section')).toBe(true);
    });

    it('detects bold', () => {
      expect(hasMarkdown('**bold text**')).toBe(true);
      expect(hasMarkdown('__bold text__')).toBe(true);
    });

    it('detects italic', () => {
      expect(hasMarkdown('*italic text*')).toBe(true);
      expect(hasMarkdown('_italic text_')).toBe(true);
    });

    it('detects code blocks', () => {
      expect(hasMarkdown('```js\ncode\n```')).toBe(true);
      expect(hasMarkdown('`inline code`')).toBe(true);
    });

    it('detects lists', () => {
      expect(hasMarkdown('- item')).toBe(true);
      expect(hasMarkdown('1. item')).toBe(true);
    });

    it('returns false for plain text', () => {
      expect(hasMarkdown('plain text')).toBe(false);
      expect(hasMarkdown('')).toBe(false);
    });
  });

  describe('renderMarkdown', () => {
    it('returns empty string for empty input', () => {
      expect(renderMarkdown('')).toBe('');
    });

    it('returns plain text for non-markdown', () => {
      expect(renderMarkdown('hello world').trim()).toBe('hello world');
    });

    it('renders bold text', () => {
      const result = renderMarkdown('**bold**');
      expect(result).toContain('bold');
    });

    it('renders code blocks', () => {
      const result = renderMarkdown('```\ncode\n```');
      expect(result).toContain('code');
    });
  });
});
