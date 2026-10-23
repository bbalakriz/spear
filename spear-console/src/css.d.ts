// plain side effect css imports (e.g. './spear-shield-chat.css') have no
// type declarations of their own, webpack's css-loader handles them fine
// at build time but tsc has nothing to resolve them against without this
declare module '*.css';
