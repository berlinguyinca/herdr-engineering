export default [
  {
    files: [".superpowers/scratch-js/**/*.js"],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: "module",
      globals: { process: "readonly" },
    },
    rules: { "no-unused-vars": "error", "no-undef": "error" },
  },
];
