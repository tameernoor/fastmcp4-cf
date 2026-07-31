// Text modules, per the "rules" block in wrangler.jsonc.
declare module "*.html" {
  const content: string;
  export default content;
}
