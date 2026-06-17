declare module 'marked-terminal' {
  import type {MarkedExtension} from 'marked';
  function markedTerminal(options?: Record<string, unknown>): MarkedExtension;
  export default markedTerminal;
}
