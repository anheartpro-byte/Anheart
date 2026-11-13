"use node";
/**
 * The operator's own console code (ANH-152), hashed here with scrypt: the
 * default runtime has WebCrypto, which has no memory-hard hash, and this one
 * has `node:crypto`. The console verifies it offline with the parameters the
 * signed list carries (`code` of `operator_list`, contracts/machine-api.json).
 *
 * The code exists in clear only in this call: it is neither stored, nor
 * logged, nor answered. Its hash is never served to the site.
 */
import { randomBytes, scrypt } from "node:crypto";
import { v } from "convex/values";
import { action } from "./_generated/server";
import { internal } from "./_generated/api";
import { refusal } from "./lib/errors";
import { isAcceptableCode, OPERATOR_CODE } from "./lib/consoleAccess";

function hashed(code: string, salt: Buffer): Promise<Buffer> {
  const { n, r, p, hash_bytes } = OPERATOR_CODE;
  return new Promise((resolve, reject) => {
    scrypt(code, salt, hash_bytes, { N: n, r, p, maxmem: 256 * n * r }, (error, key) =>
      error === null ? resolve(key) : reject(error),
    );
  });
}

/**
 * Set the caller's console code, for every machine of their organisation they
 * are an operator of. Their write limit and their right to one are checked
 * before anything is hashed; a code of the wrong form is refused
 * (`invalid_operator_code`) with nothing written.
 */
export const setMyOperatorCode = action({
  args: { code: v.string() },
  returns: v.null(),
  handler: async (ctx, args) => {
    await ctx.runMutation(internal.consoleAccess.prepareOperatorCode, {});
    if (!isAcceptableCode(args.code)) throw refusal("invalid_operator_code");
    const salt = randomBytes(OPERATOR_CODE.salt_bytes);
    const hash = await hashed(args.code, salt);
    await ctx.runMutation(internal.consoleAccess.storeOperatorCode, {
      code: {
        algorithm: "scrypt",
        n: OPERATOR_CODE.n,
        r: OPERATOR_CODE.r,
        p: OPERATOR_CODE.p,
        salt: salt.toString("base64"),
        hash: hash.toString("base64"),
      },
    });
    return null;
  },
});
