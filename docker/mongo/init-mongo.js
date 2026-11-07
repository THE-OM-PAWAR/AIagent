// MongoDB entrypoint initialization script for AIagent
// Creates an application-specific user with readWrite privileges on the target database.

const dbName = process.env.MONGO_INITDB_DATABASE || 'telegram_ai_bot';
const appUser = process.env.MONGO_APP_USERNAME || 'aiagent_user';
const appPassword = process.env.MONGO_APP_PASSWORD || 'aiagent_password';

const targetDb = db.getSiblingDB(dbName);

targetDb.createUser({
  user: appUser,
  pwd: appPassword,
  roles: [
    { role: 'readWrite', db: dbName },
    { role: 'dbAdmin', db: dbName }
  ]
});

print(`[MongoDB Init] Successfully initialized application database '${dbName}' and user '${appUser}'.`);

