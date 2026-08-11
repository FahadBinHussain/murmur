import { pgTable, text, timestamp } from "drizzle-orm/pg-core";

export const steamSeen = pgTable("steam_seen", {
  gid: text("gid").primaryKey(),
  gameName: text("game_name").notNull(),
  title: text("title").notNull(),
  seenAt: timestamp("seen_at", { withTimezone: true }).notNull().defaultNow(),
});

export const gameSeen = pgTable("game_seen", {
  guid: text("guid").primaryKey(),
  title: text("title").notNull(),
  seenAt: timestamp("seen_at", { withTimezone: true }).notNull().defaultNow(),
});
