// Baseline check for CSS: warns about features not yet "widely available"
// in browsers (web-features Baseline), the measurable part of modern-web
// guidance. Warning only.
import css from "@eslint/css";

const ROOT = process.env.KIT_LINT_ROOT || process.cwd();

export default [
  { basePath: ROOT, ignores: ["**/node_modules/**", "**/dist/**", "**/vendor/**", "**/*.min.css", "**/assets/**"] },
  {
    basePath: ROOT,
    files: ["**/*.css"],
    language: "css/css",
    plugins: { css },
    languageOptions: { tolerant: true },
    rules: { "css/use-baseline": ["warn", { available: "widely" }] },
  },
];
