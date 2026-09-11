import js from "@eslint/js";
import globals from "globals";
export default [
  js.configs.recommended,
  { files: ["assets/dashboard/*.js"], languageOptions: { globals: globals.browser } },
  { files: ["scripts/*.cjs", "*.mjs"], languageOptions: { globals: globals.node } },
];
