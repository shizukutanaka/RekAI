// Flat config for ESLint 9 — `next lint` and .eslintrc were removed in Next 16.
import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";

const eslintConfig = defineConfig([
  ...nextVitals,
  globalIgnores([".next/**", "out/**", "next-env.d.ts", "playwright-report/**"]),
]);

export default eslintConfig;
