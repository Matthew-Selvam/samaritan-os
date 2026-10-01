/**
 * Ambient declaration for `cytoscape-cose-bilkent`.
 *
 * The package ships plain JS with no type definitions. This gives the dynamic
 * `import()` in `components/EntityGraph.tsx` a precise shape so the layout
 * registration stays type-safe instead of degrading to `any`.
 */
declare module "cytoscape-cose-bilkent" {
  import type { Core } from "cytoscape";

  /** Registers the `cose-bilkent` layout on Cytoscape's prototype. */
  const register: (cy: Core) => void;
  export default register;
}