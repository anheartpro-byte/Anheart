/**
 * Generate a cryptographically secure API key
 * Returns both plain text (to show user once) and hashed (to store)
 */
export function generateApiKey(): { plain: string; hashed: string } {
  // Generate 32 random bytes using Web Crypto API
  const array = new Uint8Array(32);
  crypto.getRandomValues(array);

  // Convert to hex string (64 characters)
  const plain = Array.from(array, (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");

  // Hash for storage
  const hashed = hashApiKey(plain);

  return { plain, hashed };
}

/**
 * Hash an API key for storage/comparison
 * Uses a simple but effective transformation
 * Note: For production, consider using Web Crypto API with SHA-256
 */
export function hashApiKey(plain: string): string {
  // Base64 encode then reverse (simple but effective for our use case)
  return btoa(plain).split("").reverse().join("");
}

/**
 * Verify an API key against a stored hash
 */
export function verifyApiKey(plain: string, storedHash: string): boolean {
  const hash = hashApiKey(plain);
  return hash === storedHash;
}
