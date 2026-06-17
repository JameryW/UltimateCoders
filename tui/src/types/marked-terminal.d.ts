declare module 'marked-terminal' {
  interface MarkedTerminalOptions {
    width?: number;
    reflowText?: boolean;
    showLink?: boolean;
    heading?: string[];
    firstHeading?: string[];
    strong?: string[];
    em?: string[];
    code?: string[];
    codespan?: string[];
    blockquote?: string[];
    listitem?: string[];
    table?: string[];
    paragraph?: string[];
  }
  function markedTerminal(options?: MarkedTerminalOptions): any;
  export default markedTerminal;
}
