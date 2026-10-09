import js from "@eslint/js";
import globals from "globals";
export default [
  js.configs.recommended,
  { files: ["assets/dashboard/*.js"], languageOptions: { globals: globals.browser } },
  {
    files: ["extension/*.js"],
    languageOptions: { globals: { ...globals.browser, chrome: "readonly" } },
  },
  {
    files: ["scripts/*.cjs", "scripts/*.mjs", "*.mjs"],
    languageOptions: { globals: globals.node },
  },
];
