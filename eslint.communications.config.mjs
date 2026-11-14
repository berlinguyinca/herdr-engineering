// TypeScript checks the source; lint the exact browser code emitted by it.
export default [
  {
    files: [
      "packages/communications-view.js",
      "packages/communications-view.test.js",
    ],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        document: "readonly",
        AbortController: "readonly",
        URLSearchParams: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
      },
    },
    rules: {
      "no-undef": "error",
      "no-unused-vars": "error",
      "no-unreachable": "error",
      "no-dupe-keys": "error",
      "no-constant-binary-expression": "error",
      eqeqeq: "error",
    },
  },
];
