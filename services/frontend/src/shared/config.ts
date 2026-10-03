export interface PublicConfig {
  googleClientId: string;
  chatApiUrl: string;
  ingestionApiUrl: string;
  localIdentity: boolean;
}
declare global {
  interface Window {
    HORIZON_CONFIG?: PublicConfig;
  }
}
export function readConfig(): PublicConfig {
  const config = window.HORIZON_CONFIG;
  if (!config) throw new Error("Public configuration is missing.");
  const loopback = (host: string) =>
    ["localhost", "127.0.0.1", "[::1]"].includes(host);
  for (const value of [config.chatApiUrl, config.ingestionApiUrl]) {
    const url = new URL(value);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    )
      throw new Error("Invalid API URL.");
    if (config.localIdentity && !loopback(url.hostname))
      throw new Error("Local identity requires loopback APIs.");
  }
  if (
    config.localIdentity &&
    (!loopback(location.hostname) || !import.meta.env.DEV)
  )
    throw new Error(
      "Local identity is available only in loopback development.",
    );
  return config;
}
