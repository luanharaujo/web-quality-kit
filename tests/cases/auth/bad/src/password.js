import bcrypt from "bcryptjs";
export const save = (password) => bcrypt.hash(password, 8);
