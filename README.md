# reddit_needs

Agent qui repère sur Reddit les posts exprimant un besoin, les fait analyser par un LLM local (vLLM, API compatible OpenAI), puis classe les besoins pouvant être satisfaits par un moyen numérique.

## Pipeline

1. `collect` : récupère les posts candidats via l'API officielle de Reddit et les stocke dans SQLite.
2. `analyze` : analyse chaque candidat avec le LLM (besoin, intensité, volonté de payer, faisabilité).
3. `report` : génère `rapport_besoins.md` et `rapport_besoins.csv`.

## Prérequis

- Python 3.9+
- Un accès à l'API Reddit **approuvé par Reddit** (voir la [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)). Un usage commercial exige une approbation écrite explicite.
- Un serveur vLLM joignable.

## Installation

```
pip install -r requirements.txt
copy .env.example .env     # puis renseigner les valeurs
```

Le fichier `.env` contient les identifiants et n'est jamais versionné.

## Usage

```
python reddit_needs_agent.py collect
python reddit_needs_agent.py analyze
python reddit_needs_agent.py report
python reddit_needs_agent.py all
```

## Données

La base `needs.db` et les rapports contiennent des données issues de Reddit : ils sont exclus du dépôt (`.gitignore`) et ne doivent pas être redistribués.

## Conformité et usage des données

Ce projet est conçu pour respecter la [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy) de Reddit :

- **Lecture seule** : aucun post, commentaire, vote ni message n'est envoyé sur Reddit.
- **Accès approuvé** : `collect` refuse de s'exécuter sans identifiants API, et ne lit pas Reddit de façon anonyme.
- **Volume limité** : environ 30 subreddits, quelques centaines de posts par exécution, avec une pause entre les requêtes (`REQUEST_DELAY_SECONDS`) et gestion des erreurs 429.
- **Pas de profilage** : aucun nom d'utilisateur n'est collecté ni stocké. Seuls le titre, le texte, le score et les commentaires sont conservés pour analyser les *besoins exprimés*, jamais les personnes. Aucune caractéristique sensible n'est déduite.
- **Pas d'entraînement de modèle** : le LLM est utilisé en inférence uniquement, sur un serveur contrôlé par l'auteur. Les données Reddit ne servent ni à entraîner, ni à affiner un modèle.
- **Pas de redistribution** : la base et les rapports restent locaux et ne sont pas publiés.
- **Conservation** : les données locales peuvent être supprimées à tout moment (suppression de `needs.db`).
