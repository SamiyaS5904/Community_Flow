"""External integrations. One concern per module.

Nothing is re-exported here on purpose: importing the package used to pull in
every service, so a broken or removed one took down anything that touched
`services.*`. Import the module you need directly.
"""
