pipeline {
  agent any

  options {
    disableConcurrentBuilds()
    timestamps()
  }

  environment {
    COMPOSE_FILE = 'docker-compose.prod.yml'
    COMPOSE_PROJECT_NAME = 'tradeback_be'
  }

  stages {
    stage('Checkout frontend') {
      steps {
        dir('TradeBack_FE') {
          git branch: 'main',
              credentialsId: 'tradeback-fe-deploy-key',
              url: 'git@github.com:Bavis0803/TradeBack_FE.git'
        }
      }
    }

    stage('Load production configuration') {
      steps {
        withCredentials([file(credentialsId: 'tradeback-production-env', variable: 'TRADEBACK_ENV')]) {
          sh 'cp "$TRADEBACK_ENV" .env && chmod 600 .env'
        }
        sh 'docker compose --env-file .env config --quiet'
      }
    }

    stage('Test backend') {
      steps {
        sh 'docker compose --env-file .env build backend'
        sh 'docker compose --env-file .env run --rm -e DB_ENGINE=sqlite -e REDIS_URL= backend python manage.py test user.tests exchanges.tests copytrading.tests --verbosity 1'
      }
    }

    stage('Test frontend') {
      steps {
        sh 'docker build --target test -t tradeback-frontend-test TradeBack_FE/tradeback'
      }
    }

    stage('Build and deploy') {
      steps {
        sh 'docker compose --env-file .env build --pull'
        sh 'docker compose --env-file .env up -d --remove-orphans'
      }
    }

    stage('Health check') {
      steps {
        sh '''
          port=$(docker compose --env-file .env port frontend 80 | awk -F: '{print $NF}')
          for i in $(seq 1 20); do
            if curl -fsS "http://127.0.0.1:$port/health/" >/dev/null; then
              index_html=$(curl -fsS "http://127.0.0.1:$port/")
              js_path=$(printf '%s' "$index_html" | grep -oE 'src="[^"]+\.js"' | head -1 | cut -d'"' -f2)
              css_path=$(printf '%s' "$index_html" | grep -oE 'href="[^"]+\.css"' | head -1 | cut -d'"' -f2)
              if [ -n "$js_path" ] && [ -n "$css_path" ] \
                && curl -fsS "http://127.0.0.1:$port$js_path" >/dev/null \
                && curl -fsS "http://127.0.0.1:$port$css_path" >/dev/null; then
                exit 0
              fi
            fi
            sleep 3
          done
          exit 1
        '''
      }
    }
  }

  post {
    failure {
      sh 'docker compose --env-file .env ps || true'
      sh 'docker compose --env-file .env logs --tail=120 backend telegram-worker frontend || true'
    }
    cleanup {
      sh 'rm -f .env'
    }
  }
}
