export const MAX_TEXT_LENGTH = 1000;
export const TEXT_LIMIT_RULE = {
  validator: (_: unknown, value?: string) =>
    value && value.length > MAX_TEXT_LENGTH
      ? Promise.reject(new Error(`最多只能输入${MAX_TEXT_LENGTH}字`))
      : Promise.resolve(),
};
