FROM node:20-alpine

WORKDIR /app

# Install dependencies first (cached layer)
COPY package.json package-lock.json* ./
RUN npm install

# Source is mounted as a volume in dev — copy is a fallback for image-only builds
COPY . .

EXPOSE 3001

CMD ["npm", "run", "dev"]
