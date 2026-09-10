/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DATAHUB_LOCAL_URL?: string;
  readonly VITE_DATAHUB_REMOTE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
