// The kit's ESLint rules for repositories without their own config.
// Not type-aware on purpose: no build or tsconfig needed, same result anywhere.
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

const ROOT = process.env.KIT_LINT_ROOT || process.cwd();

export default tseslint.config(
  {
    basePath: ROOT,
    ignores: ["**/node_modules/**", "**/dist/**", "**/vendor/**", "**/*.min.js", "**/.wrangler/**",
              "**/.lighthouseci/**", "**/assets/*-*.js"],
  },
  {
    basePath: ROOT,
    files: ["**/*.{js,mjs,cjs,ts,tsx,jsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    plugins: { "react-hooks": reactHooks },
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: { ...globals.browser, ...globals.node, ...globals.serviceworker },
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // An unused "catch (e)" or "_arg" is a choice, not a bug.
      "@typescript-eslint/no-unused-vars": ["error", { caughtErrors: "none", argsIgnorePattern: "^_",
                                                       varsIgnorePattern: "^_" }],
      complexity: ["warn", 15],
      "max-lines-per-function": ["warn", { max: 120, skipBlankLines: true, skipComments: true }],
      "max-depth": ["warn", 4],
      "max-params": ["warn", 5],
      "max-lines": ["warn", { max: 800, skipBlankLines: true, skipComments: true }],
    },
  },
);
