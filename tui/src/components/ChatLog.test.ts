import {describe, it, expect} from 'vitest';
import {createUserMessage, createSystemMessage} from './ChatLog.js';

// ── createUserMessage ────────────────────────────────────────

describe('createUserMessage', () => {
  it('returns message with isUser=true', () => {
    const msg = createUserMessage('hello');
    expect(msg.isUser).toBe(true);
  });

  it('sets text correctly', () => {
    const msg = createUserMessage('hello world');
    expect(msg.text).toBe('hello world');
  });

  it('generates id starting with "user-"', () => {
    const msg = createUserMessage('test');
    expect(msg.id).toMatch(/^user-/);
  });

  it('generates timestamp in HH:MM format', () => {
    const msg = createUserMessage('test');
    // HH:MM is exactly 5 characters
    expect(msg.timestamp).toMatch(/^\d{2}:\d{2}$/);
  });

  it('produces unique ids across calls', () => {
    const msg1 = createUserMessage('a');
    const msg2 = createUserMessage('b');
    expect(msg1.id).not.toBe(msg2.id);
  });

  it('does not set color, bold, dim, or eventType', () => {
    const msg = createUserMessage('test');
    expect(msg.color).toBeUndefined();
    expect(msg.bold).toBeUndefined();
    expect(msg.dim).toBeUndefined();
    expect(msg.eventType).toBeUndefined();
  });
});

// ── createSystemMessage ──────────────────────────────────────

describe('createSystemMessage', () => {
  it('returns message with isUser=false', () => {
    const msg = createSystemMessage('done');
    expect(msg.isUser).toBe(false);
  });

  it('sets text correctly', () => {
    const msg = createSystemMessage('task completed');
    expect(msg.text).toBe('task completed');
  });

  it('generates id starting with "sys-"', () => {
    const msg = createSystemMessage('test');
    expect(msg.id).toMatch(/^sys-/);
  });

  it('generates timestamp in HH:MM format', () => {
    const msg = createSystemMessage('test');
    expect(msg.timestamp).toMatch(/^\d{2}:\d{2}$/);
  });

  it('produces unique ids across calls', () => {
    const msg1 = createSystemMessage('a');
    const msg2 = createSystemMessage('b');
    expect(msg1.id).not.toBe(msg2.id);
  });

  it('merges color option', () => {
    const msg = createSystemMessage('err', {color: 'red'});
    expect(msg.color).toBe('red');
  });

  it('merges bold option', () => {
    const msg = createSystemMessage('important', {bold: true});
    expect(msg.bold).toBe(true);
  });

  it('merges dim option', () => {
    const msg = createSystemMessage('detail', {dim: true});
    expect(msg.dim).toBe(true);
  });

  it('merges multiple options', () => {
    const msg = createSystemMessage('err', {color: 'red', bold: true, dim: false});
    expect(msg.color).toBe('red');
    expect(msg.bold).toBe(true);
    expect(msg.dim).toBe(false);
  });

  it('without options has undefined color/bold/dim', () => {
    const msg = createSystemMessage('plain');
    expect(msg.color).toBeUndefined();
    expect(msg.bold).toBeUndefined();
    expect(msg.dim).toBeUndefined();
  });

  it('without options has undefined eventType', () => {
    const msg = createSystemMessage('plain');
    expect(msg.eventType).toBeUndefined();
  });
});
